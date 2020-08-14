import datetime as _datetime
import uuid as _uuid
from typing import List, Dict, Any

import six as _six
from six.moves import queue as _queue
from  flyteidl.core import workflow_pb2 as _core_workflow_pb2

from flytekit.common import constants as _constants
from flytekit.common import interface as _interface, nodes as _nodes, sdk_bases as _sdk_bases, \
    launch_plan as _launch_plan, promise as _promise
from flytekit.common.core import identifier as _identifier
from flytekit.common.exceptions import scopes as _exception_scopes, user as _user_exceptions
from flytekit.common.exceptions import system as _system_exceptions
from flytekit.common.mixins import registerable as _registerable, hash as _hash_mixin
from flytekit.common.types import helpers as _type_helpers
from flytekit.configuration import internal as _internal_config, platform as _platform_config
from flytekit.engines.flyte import engine as _flyte_engine
from flytekit.models import interface as _interface_models, literals as _literal_models, common as _common_models, \
    schedule as _schedule_models, launch_plan as _launch_plan_models
from flytekit.models.admin import workflow as _admin_workflow_model
from flytekit.models.core import workflow as _workflow_models, identifier as _identifier_model


class Output(object):

    def __init__(self, name, value, sdk_type=None, help=None):
        """
        :param Text name:
        :param T value:
        :param U sdk_type: If specified, the value provided must cast to this type.  Normally should be an instance of
            flytekit.common.types.base_sdk_types.FlyteSdkType.  But could also be something like:

            list[flytekit.common.types.base_sdk_types.FlyteSdkType],
            dict[flytekit.common.types.base_sdk_types.FlyteSdkType,flytekit.common.types.base_sdk_types.FlyteSdkType],
            (flytekit.common.types.base_sdk_types.FlyteSdkType, flytekit.common.types.base_sdk_types.FlyteSdkType, ...)
        """
        if sdk_type is None:
            # This syntax didn't work for some reason: sdk_type = sdk_type or Output._infer_type(value)
            sdk_type = Output._infer_type(value)
        sdk_type = _type_helpers.python_std_to_sdk_type(sdk_type)

        self._binding_data = _interface.BindingData.from_python_std(sdk_type.to_flyte_literal_type(), value)
        self._var = _interface_models.Variable(sdk_type.to_flyte_literal_type(), help or '')
        self._name = name

    def rename_and_return_reference(self, new_name):
        self._name = new_name
        return self

    @staticmethod
    def _infer_type(value):
        # TODO: Infer types
        raise NotImplementedError("Currently the SDK cannot infer a workflow output type, so please use the type kwarg "
                                  "when instantiating an output.")

    @property
    def name(self):
        """
        :rtype: Text
        """
        return self._name

    @property
    def binding_data(self):
        """
        :rtype: flytekit.models.literals.BindingData
        """
        return self._binding_data

    @property
    def var(self):
        """
        :rtype: flytekit.models.interface.Variable
        """
        return self._var


class SdkWorkflow(
    _hash_mixin.HashOnReferenceMixin,
    _workflow_models.WorkflowTemplate,
    _registerable.RegisterableEntity,
    metaclass=_sdk_bases.ExtendedSdkType,
):
    """
    Previously this class represented both local and control plane constructs. As of this writing, we are making this
    class only a control plane class. Workflow constructs that rely on local code being present have been moved to
    the new PythonWorkflow class.
    """

    def __init__(self, inputs, outputs, nodes, id=None, metadata=None, metadata_defaults=None,
                 interface=None, output_bindings=None):
        """
        :param list[flytekit.common.promise.Input] inputs:
        :param list[Output] outputs:
        :param list[flytekit.common.nodes.SdkNode] nodes:
        :param flytekit.models.core.identifier.Identifier id: This is an autogenerated id by the system. The id is
            globally unique across Flyte.
        :param WorkflowMetadata metadata: This contains information on how to run the workflow.
        :param flytekit.models.core.workflow.WorkflowMetadataDefaults metadata_defaults: Defaults to be passed
            to nodes contained within workflow.
        :param flytekit.models.interface.TypedInterface interface: Defines a strongly typed interface for the
            Workflow (inputs, outputs).  This can include some optional parameters.
        :param list[flytekit.models.literals.Binding] output_bindings: A list of output bindings that specify how to construct
            workflow outputs. Bindings can pull node outputs or specify literals. All workflow outputs specified in
            the interface field must be bound
            in order for the workflow to be validated. A workflow has an implicit dependency on all of its nodes
            to execute successfully in order to bind final outputs.
        """
        for n in nodes:
            for upstream in n.upstream_nodes:
                if upstream.id is None:
                    raise _user_exceptions.FlyteAssertion(
                        "Some nodes contained in the workflow were not found in the workflow description.  Please "
                        "ensure all nodes are either assigned to attributes within the class or an element in a "
                        "list, dict, or tuple which is stored as an attribute in the class."
                    )

        # Allow overrides if specified for all the arguments to the parent class constructor
        id = id if id is not None else _identifier.Identifier(
            _identifier_model.ResourceType.WORKFLOW,
            _internal_config.PROJECT.get(),
            _internal_config.DOMAIN.get(),
            _uuid.uuid4().hex,
            _internal_config.VERSION.get()
        )
        metadata = metadata if metadata is not None else _workflow_models.WorkflowMetadata()
        metadata_defaults = metadata_defaults if metadata_defaults \
                                                 is not None else _workflow_models.WorkflowMetadataDefaults()

        interface = interface if interface is not None else _interface.TypedInterface(
            {v.name: v.var for v in inputs},
            {v.name: v.var for v in outputs}
        )

        output_bindings = output_bindings if output_bindings is not None else \
            [_literal_models.Binding(v.name, v.binding_data) for v in outputs]

        super(SdkWorkflow, self).__init__(
            id=id,
            metadata=metadata,
            metadata_defaults=metadata_defaults,
            interface=interface,
            nodes=nodes,
            outputs=output_bindings,
        )
        self._user_inputs = inputs

    @property
    def interface(self):
        """
        :rtype: flytekit.common.interface.TypedInterface
        """
        return super(SdkWorkflow, self).interface

    @property
    def entity_type_text(self):
        """
        :rtype: Text
        """
        return "Workflow"

    @property
    def resource_type(self):
        """
        Integer from _identifier.ResourceType enum
        :rtype: int
        """
        return _identifier_model.ResourceType.WORKFLOW

    def get_sub_workflows(self):
        """
        Recursive call that returns all subworkflows in the current workflow

        :rtype: list[SdkWorkflow]
        """
        result = []
        for n in self.nodes:
            if n.workflow_node is not None and n.workflow_node.sub_workflow_ref is not None:
                if n.executable_sdk_object is not None and n.executable_sdk_object.entity_type_text == 'Workflow':
                    result.append(n.executable_sdk_object)
                    result.extend(n.executable_sdk_object.get_sub_workflows())
                else:
                    raise _system_exceptions.FlyteSystemException(
                        "workflow node with subworkflow found but bad executable "
                        "object {}".format(n.executable_sdk_object))
            # Ignore other node types (branch, task)

        return result

    @classmethod
    @_exception_scopes.system_entry_point
    def fetch(cls, project, domain, name, version=None):
        """
        This function uses the engine loader to call create a hydrated task from Admin.
        :param Text project:
        :param Text domain:
        :param Text name:
        :param Text version:
        :rtype: SdkWorkflow
        """
        version = version or _internal_config.VERSION.get()
        workflow_id = _identifier.Identifier(_identifier_model.ResourceType.WORKFLOW, project, domain, name, version)
        admin_workflow = _flyte_engine._FlyteClientManager(
            _platform_config.URL.get(),
            insecure=_platform_config.INSECURE.get()
        ).client.get_workflow(workflow_id)
        cwc = admin_workflow.closure.compiled_workflow
        primary_template = cwc.primary.template
        sub_workflow_map = {sw.template.id: sw.template for sw in cwc.sub_workflows}
        task_map = {t.template.id: t.template for t in cwc.tasks}
        sdk_workflow = cls.promote_from_model(primary_template, sub_workflow_map, task_map)
        sdk_workflow._id = workflow_id
        return sdk_workflow

    @classmethod
    def get_non_system_nodes(cls, nodes):
        """
        :param list[flytekit.models.core.workflow.Node] nodes:
        :rtype: list[flytekit.models.core.workflow.Node]
        """
        return [n for n in nodes if n.id not in {_constants.START_NODE_ID, _constants.END_NODE_ID}]

    @classmethod
    def promote_from_model(cls, base_model, sub_workflows=None, tasks=None):
        """
        :param flytekit.models.core.workflow.WorkflowTemplate base_model:
        :param dict[flytekit.models.core.identifier.Identifier, flytekit.models.core.workflow.WorkflowTemplate]
            sub_workflows: Provide a list of WorkflowTemplate
            models (should be returned from Admin as part of the admin CompiledWorkflowClosure. Relevant sub-workflows
            should always be provided.
        :param dict[flytekit.models.core.identifier.Identifier, flytekit.models.task.TaskTemplate] tasks: Same as above
            but for tasks. If tasks are not provided relevant TaskTemplates will be fetched from Admin
        :rtype: SdkWorkflow
        """
        base_model_non_system_nodes = cls.get_non_system_nodes(base_model.nodes)
        sub_workflows = sub_workflows or {}
        tasks = tasks or {}
        node_map = {n.id: _nodes.SdkNode.promote_from_model(n, sub_workflows, tasks)
                    for n in base_model_non_system_nodes}

        # Set upstream nodes for each node
        for n in base_model_non_system_nodes:
            current = node_map[n.id]
            for upstream_id in current.upstream_node_ids:
                upstream_node = node_map[upstream_id]
                current << upstream_node

        # No inputs/outputs specified, see the constructor for more information on the overrides.
        return cls(
            inputs=None, outputs=None, nodes=list(node_map.values()),
            id=_identifier.Identifier.promote_from_model(base_model.id),
            metadata=base_model.metadata,
            metadata_defaults=base_model.metadata_defaults,
            interface=_interface.TypedInterface.promote_from_model(base_model.interface),
            output_bindings=base_model.outputs,
        )

    @_exception_scopes.system_entry_point
    def register(self, project, domain, name, version):
        """
        :param Text project:
        :param Text domain:
        :param Text name:
        :param Text version:
        """
        self.validate()
        id_to_register = _identifier.Identifier(
            _identifier_model.ResourceType.WORKFLOW,
            project,
            domain,
            name,
            version
        )
        old_id = self.id
        self._id = id_to_register
        try:
            client = _flyte_engine._FlyteClientManager(_platform_config.URL.get(), insecure=_platform_config.INSECURE.get()).client
            sub_workflows = self.get_sub_workflows()
            client.create_workflow(
                id_to_register,
                _admin_workflow_model.WorkflowSpec(
                    self,
                    sub_workflows,
                )
            )
            self._id = id_to_register
            return str(id_to_register)
        except _user_exceptions.FlyteEntityAlreadyExistsException:
            pass
        except Exception:
            self._id = old_id
            raise

    @_exception_scopes.system_entry_point
    def serialize(self):
        """
        Serializing a workflow should produce an object similar to what the registration step produces, in preparation
        for actual registration to Admin.

        :rtype: flyteidl.admin.workflow_pb2.WorkflowSpec
        """
        sub_workflows = self.get_sub_workflows()
        return _admin_workflow_model.WorkflowSpec(
            self,
            sub_workflows,
        ).to_flyte_idl()

    @_exception_scopes.system_entry_point
    def validate(self):
        pass

    # TODO: Should we just get rid of this function for now and raise an Exception? We probably should.
    @_exception_scopes.system_entry_point
    def create_launch_plan(
            self,
            default_inputs: Dict[str, _interface_models.Parameter] = None,
            fixed_inputs: Dict[str, Any] = None,
            schedule: _schedule_models.Schedule = None,
            notifications=None,
            labels=None,
            annotations=None,
            auth_role: _common_models.AuthRole = None,
    ):
        """
        This method will create a launch plan object that can execute this workflow.
        :param dict[Text,flytekit.common.promise.Input] default_inputs:
        :param dict[Text,T] fixed_inputs:
        :param flytekit.models.schedule.Schedule schedule: A schedule on which to execute this launch plan.
        :param list[flytekit.models.common.Notification] notifications: A list of notifications to enact by default for
        this launch plan.
        :param flytekit.models.common.Labels labels:
        :param flytekit.models.common.Annotations annotations:
        :param cls: This parameter can be used by users to define an extension of a launch plan to instantiate.  The
        class provided should be a subclass of flytekit.common.launch_plan.SdkLaunchPlan.
        :param auth_role: Auth object
        :rtype: flytekit.common.launch_plan.SdkRunnableLaunchPlan
        """
        # TODO: Actually ensure the parameters conform.
        # Determine fixed inputs
        fixed_inputs = fixed_inputs or {}
        fixed_launch_plan_inputs = _type_helpers.pack_python_std_map_to_literal_map(
            fixed_inputs,
            {
                k: _type_helpers.get_sdk_type_from_literal_type(var.type)
                for k, var in _six.iteritems(self.interface.inputs) if k in fixed_inputs
            }
        )

        return _launch_plan.SdkLaunchPlan(
            workflow_id=None,  # One could be calling this anywhere, can't assume an ID.
            entity_metadata=_launch_plan_models.LaunchPlanMetadata(
                schedule=schedule or _schedule_models.Schedule(''),
                notifications=notifications or []
            ),
            default_inputs=_interface_models.ParameterMap(default_inputs),
            fixed_inputs=fixed_launch_plan_inputs,
            labels=labels or _common_models.Labels({}),
            annotations=annotations or _common_models.Annotations({}),
            auth_role=auth_role,
        )

    @_exception_scopes.system_entry_point
    def __call__(self, *args, **input_map):
        if len(args) > 0:
            raise _user_exceptions.FlyteAssertion(
                "When adding a workflow as a node in a workflow, all inputs must be specified with kwargs only.  We "
                "detected {} positional args.".format(len(args))
            )

        bindings, upstream_nodes = self.interface.create_bindings_for_inputs(input_map)

        node = _nodes.SdkNode(
            id=None,
            metadata=_workflow_models.NodeMetadata("placeholder", _datetime.timedelta(),
                                                   _literal_models.RetryStrategy(0)),
            upstream_nodes=upstream_nodes,
            bindings=sorted(bindings, key=lambda b: b.var),
            sdk_workflow=self
        )
        return node


class PythonWorkflow(_hash_mixin.HashOnReferenceMixin, _registerable.LocalEntity, _registerable.RegisterableEntity):
    """
    Wrapper class for locally defined Python workflows
    """

    def __init__(self, workflow_function, flyte_workflow: SdkWorkflow, inputs: List[_promise.Input],
                 nodes: List[_nodes.SdkNode]):
        self._workflow_function = workflow_function
        # Currently doing a has-a relationship, cuz it's easier to work with while refactoring, can revisit later.
        self._flyte_workflow = flyte_workflow
        self._workflow_inputs = inputs
        self._nodes = nodes
        self._user_inputs = inputs
        self._upstream_entities = set(n.executable_sdk_object for n in nodes)

    def __call__(self, *args, **input_map):
        # Take the default values from the Inputs
        compiled_inputs = {
            v.name: v.sdk_default
            for v in self.user_inputs if not v.sdk_required
        }
        compiled_inputs.update(input_map)
        # import ipdb; ipdb.set_trace()

        return self.flyte_workflow.__call__(*args, **compiled_inputs)

    @property
    def flyte_workflow(self) -> SdkWorkflow:
        return self._flyte_workflow

    @classmethod
    def construct_from_class_definition(cls, inputs: List[_promise.Input], outputs: List[Output],
                                        nodes: List[_nodes.SdkNode],
                                        metadata: _workflow_models.WorkflowMetadata = None,
                                        metadata_defaults: _workflow_models.WorkflowMetadataDefaults = None
                                        ) -> 'PythonWorkflow':
        """
        This constructor is here to provide backwards-compatibility for class-defined Workflows

        :param inputs:
        :param outputs:
        :param nodes:
        :param metadata:
        :param metadata_defaults:
        :rtype: PythonWorkflow
        """
        for n in nodes:
            for upstream in n.upstream_nodes:
                if upstream.id is None:
                    raise _user_exceptions.FlyteAssertion(
                        "Some nodes contained in the workflow were not found in the workflow description.  Please "
                        "ensure all nodes are either assigned to attributes within the class or an element in a "
                        "list, dict, or tuple which is stored as an attribute in the class."
                    )

        id = _identifier.Identifier(
            _identifier_model.ResourceType.WORKFLOW,
            _internal_config.PROJECT.get(),
            _internal_config.DOMAIN.get(),
            _uuid.uuid4().hex,
            _internal_config.VERSION.get()
        )
        interface = _interface.TypedInterface(
            {v.name: v.var for v in inputs},
            {v.name: v.var for v in outputs}
        )

        output_bindings = [_literal_models.Binding(v.name, v.binding_data) for v in outputs]

        sdk_workflow = SdkWorkflow(
            inputs=None,
            outputs=None,
            id=id,
            metadata=metadata,
            metadata_defaults=metadata_defaults,
            interface=interface,
            nodes=nodes,
            output_bindings=output_bindings,
        )

        return cls(None, sdk_workflow, inputs, nodes)

    @property
    def nodes(self):
        return self.flyte_workflow.nodes

    @property
    def outputs(self):
        return self.flyte_workflow.outputs

    @property
    def upstream_entities(self):
        # TODO: Should we re-evaluate every time?
        # return set(n.executable_sdk_object for n in self.nodes)
        return self._upstream_entities

    @property
    def interface(self):
        return self.flyte_workflow.interface

    @property
    def id(self):
        return self.flyte_workflow.id

    @id.setter
    def id(self, new_id):
        self._flyte_workflow._id = new_id

    def register(self, *args, **kwargs):
        return self.flyte_workflow.register(*args, **kwargs)

    @property
    def user_inputs(self) -> List[_promise.Input]:
        """
        :rtype: list[flytekit.common.promise.Input]
        """
        return self._user_inputs

    def create_launch_plan(
            self,
            default_inputs: Dict[str, _promise.Input] = None,
            fixed_inputs: Dict[str, Any] = None,
            schedule=None,
            role=None,
            notifications=None,
            labels=None,
            annotations=None,
            assumable_iam_role=None,
            kubernetes_service_account=None,
    ):
        """
        This method will create a launch plan object that can execute this workflow.
        :param dict[Text,flytekit.common.promise.Input] default_inputs:
        :param dict[Text,T] fixed_inputs:
        :param flytekit.models.schedule.Schedule schedule: A schedule on which to execute this launch plan.
        :param Text role: Deprecated. Use assumable_iam_role instead.
        :param list[flytekit.models.common.Notification] notifications: A list of notifications to enact by default for
        this launch plan.
        :param flytekit.models.common.Labels labels:
        :param flytekit.models.common.Annotations annotations:
        :param cls: This parameter can be used by users to define an extension of a launch plan to instantiate.  The
        class provided should be a subclass of flytekit.common.launch_plan.SdkLaunchPlan.
        :param Text assumable_iam_role: The IAM role to execute the workflow with.
        :param Text kubernetes_service_account: The kubernetes service account to execute the workflow with.
        :rtype: flytekit.common.launch_plan.SdkRunnableLaunchPlan
        """
        # TODO: Actually ensure the parameters conform.
        if role and (assumable_iam_role or kubernetes_service_account):
            raise ValueError("Cannot set both role and auth. Role is deprecated, use auth instead.")
        fixed_inputs = fixed_inputs or {}
        merged_default_inputs = {v.name: v for v in self._workflow_inputs if v.name not in fixed_inputs}
        merged_default_inputs.update(default_inputs or {})

        if role:
            assumable_iam_role = role  # For backwards compatibility
        auth_role = _common_models.AuthRole(assumable_iam_role=assumable_iam_role,
                                            kubernetes_service_account=kubernetes_service_account)

        return _launch_plan.SdkRunnableLaunchPlan(
            sdk_workflow=self,
            default_inputs={
                k: user_input.rename_and_return_reference(k)
                for k, user_input in _six.iteritems(merged_default_inputs)
            },
            fixed_inputs=fixed_inputs,
            schedule=schedule,
            notifications=notifications,
            labels=labels,
            annotations=annotations,
            auth_role=auth_role,
        )

    def to_flyte_idl(self)-> _core_workflow_pb2.WorkflowTemplate:
        return self.flyte_workflow.to_flyte_idl()


def build_sdk_workflow_from_metaclass(metaclass, on_failure=None):
    """
    :param T metaclass: This is the user-defined workflow class, prior to decoration.
    :param on_failure flytekit.models.core.workflow.WorkflowMetadata.OnFailurePolicy: [Optional] The execution policy when the workflow detects a failure.
    :rtype: SdkWorkflow
    """
    inputs, outputs, nodes = _discover_workflow_components(metaclass)
    metadata = _workflow_models.WorkflowMetadata(on_failure=on_failure if on_failure else None)

    return PythonWorkflow.construct_from_class_definition(
        inputs=[i for i in sorted(inputs, key=lambda x: x.name)],
        outputs=[o for o in sorted(outputs, key=lambda x: x.name)],
        nodes=[n for n in sorted(nodes, key=lambda x: x.id)],
        metadata=metadata,
    )


def _assign_indexed_attribute_name(attribute_name, index):
    return "{}[{}]".format(attribute_name, index)


def _discover_workflow_components(workflow_class):
    """
    This task iterates over the attributes of a user-defined class in order to return a list of inputs, outputs and
    nodes.
    :param class workflow_class: User-defined class with task instances as attributes.
    :rtype: (list[flytekit.common.promise.Input], list[Output], list[flytekit.common.nodes.SdkNode])
    """

    inputs = []
    outputs = []
    nodes = []

    to_visit_objs = _queue.Queue()
    top_level_attributes = set()
    for attribute_name in dir(workflow_class):
        to_visit_objs.put((attribute_name, getattr(workflow_class, attribute_name)))
        top_level_attributes.add(attribute_name)

    # For all task instances defined within the workflow, bind them to this specific workflow and hook-up to the
    # engine (when available)
    visited_obj_ids = set()
    while not to_visit_objs.empty():
        attribute_name, current_obj = to_visit_objs.get()

        current_obj_id = id(current_obj)
        if current_obj_id in visited_obj_ids:
            continue
        visited_obj_ids.add(current_obj_id)

        if isinstance(current_obj, _nodes.SdkNode):
            # TODO: If an attribute name is on the form node_name[index], the resulting
            # node name might not be correct.
            nodes.append(current_obj.assign_id_and_return(attribute_name))
        elif isinstance(current_obj, _promise.Input):
            if attribute_name is None or attribute_name not in top_level_attributes:
                raise _user_exceptions.FlyteValueException(
                    attribute_name,
                    "Detected workflow input specified outside of top level."
                )
            inputs.append(current_obj.rename_and_return_reference(attribute_name))
        elif isinstance(current_obj, Output):
            if attribute_name is None or attribute_name not in top_level_attributes:
                raise _user_exceptions.FlyteValueException(
                    attribute_name,
                    "Detected workflow output specified outside of top level."
                )
            outputs.append(current_obj.rename_and_return_reference(attribute_name))
        elif isinstance(current_obj, list) or isinstance(current_obj, set) or isinstance(current_obj, tuple):
            for idx, value in enumerate(current_obj):
                to_visit_objs.put(
                    (_assign_indexed_attribute_name(attribute_name, idx), value))
        elif isinstance(current_obj, dict):
            # Visit dictionary keys.
            for key in current_obj.keys():
                to_visit_objs.put(
                    (_assign_indexed_attribute_name(attribute_name, key), key))
            # Visit dictionary values.
            for key, value in _six.iteritems(current_obj):
                to_visit_objs.put(
                    (_assign_indexed_attribute_name(attribute_name, key), value))
    return inputs, outputs, nodes