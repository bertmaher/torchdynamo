import inspect
import types
from typing import Dict
from typing import List

import torch._C

from .. import variables
from ..bytecode_transformation import create_instruction
from ..exc import unimplemented
from ..guards import Guard
from ..guards import GuardBuilder
from ..guards import GuardSource
from ..source import AttrSource
from ..utils import identity
from .base import VariableTracker


class SuperVariable(VariableTracker):
    def __init__(self, typevar, objvar=None, **kwargs):
        super(SuperVariable, self).__init__(**kwargs)
        self.typevar = typevar
        self.objvar = objvar

    def reconstruct(self, codegen):
        codegen(variables.BuiltinVariable(super))
        codegen(self.typevar)
        if self.objvar is not None:
            codegen(self.objvar)
            return [create_instruction("CALL_FUNCTION", 2)]
        else:
            return [create_instruction("CALL_FUNCTION", 1)]

    def const_getattr(self, tx, name):
        assert self.objvar, "1-arg super not implemented"
        search_type = self.typevar.as_python_constant()
        # TODO(jansel): there is a small chance this could trigger user code, prevent that
        return getattr(super(search_type, self.objvar.python_type()), name)

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(
            self, args, kwargs.values(), self.objvar, self.typevar
        )
        inner_fn = self.const_getattr(self, name)
        if inner_fn is object.__init__:
            return LambdaVariable(identity, **options)
        if not isinstance(inner_fn, types.FunctionType):
            unimplemented(f"non-function super: {inner_fn}")
        return variables.UserFunctionVariable(inner_fn, **options).call_function(
            tx, [self.objvar] + args, kwargs
        )


class UnknownVariable(VariableTracker):
    """
    It could be anything!
    """


class ClosureVariable(UnknownVariable):
    def __init__(self, name, **kwargs):
        super(ClosureVariable, self).__init__(**kwargs)
        self.name = name

    def reconstruct(self, codegen):
        return [codegen.create_load_closure(self.name)]


class NewCellVariable(VariableTracker):
    def __init__(self, **kwargs):
        super(NewCellVariable, self).__init__(**kwargs)


class ContextManagerVariable(VariableTracker):
    pass


class GradModeVariable(ContextManagerVariable):
    """represents torch.{no_grad,enable_grad,set_grad_mode}()"""

    _guards_singleton = {Guard("", GuardSource.GLOBAL, GuardBuilder.GRAD_MODE)}

    def __init__(self, target_mode, original_mode=None, **kwargs):
        super(GradModeVariable, self).__init__(**kwargs)
        self.guards = self.guards | self._guards_singleton
        self.target_mode = target_mode
        if original_mode is None:
            original_mode = torch.is_grad_enabled()
        self.original_mode = original_mode

    def enter(self, tx):
        assert self.original_mode == torch.is_grad_enabled()
        if self.target_mode != self.original_mode:
            self._change_mode(tx, self.target_mode)
        return variables.ConstantVariable(None, **VariableTracker.propagate(self))

    def exit(self, tx, *args):
        if self.target_mode != self.original_mode:
            self._change_mode(tx, self.original_mode)
        return variables.ConstantVariable(None, **VariableTracker.propagate(self))

    def fn_name(self):
        if self.target_mode:
            return "enable_grad"
        else:
            return "no_grad"

    @staticmethod
    def _change_mode(tx, value):
        tx.output.graph.create_node(
            "call_function", torch._C._set_grad_enabled, (value,), {}
        ),
        torch._C._set_grad_enabled(value)


class WithExitFunctionVariable(VariableTracker):
    def __init__(self, ctx: VariableTracker, target, **kwargs):
        super(WithExitFunctionVariable, self).__init__(**kwargs)
        self.ctx = ctx
        self.target = target

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        assert not kwargs
        return self.ctx.exit(tx, *args)

    def reconstruct(self, codegen):
        # Note here we reconstruct the context manager rather than the
        # exit function.  The handler generated by BlockStackEntry
        # will re-enter the context in the resume function.
        output = AttrSource(
            codegen.tx.import_source("torch"), self.ctx.fn_name()
        ).reconstruct(codegen)

        if codegen.tx.output.partial_convert:
            output.extend(
                [
                    create_instruction("CALL_FUNCTION", 0),
                    create_instruction("SETUP_WITH", target=self.target),
                    create_instruction("POP_TOP"),
                ]
            )

        return output


class InspectSignatureVariable(VariableTracker):
    """represents inspect.signature(...)"""

    @staticmethod
    def create(callable, **kwargs):
        if kwargs:
            unimplemented(f"inspect.signature with {kwargs}")
        return InspectSignatureVariable(callable)

    def __init__(self, inspected, **kwargs):
        super(InspectSignatureVariable, self).__init__(**kwargs)
        self.inspected = inspected


class AutogradFunctionVariable(VariableTracker):
    """represents a torch.autograd.Function subclass"""

    def __init__(self, fn_cls, **kwargs):
        super().__init__(**kwargs)
        self.fn_cls = fn_cls

    def call_apply(self, tx, args, kwargs):
        requires_grad = False

        def visit(node):
            nonlocal requires_grad
            if isinstance(node, variables.TensorVariable):
                if node.requires_grad is not False:
                    requires_grad = True
            if isinstance(node, variables.NNModuleVariable):
                if node.is_training(tx):
                    requires_grad = True
            return node

        VariableTracker.apply(visit, (args, kwargs))

        if requires_grad and torch.is_grad_enabled():
            # TODO(jansel): handle this in training mode
            unimplemented("autograd.Function with requires_grad")

        args = [BlackHoleVariable()] + list(args)
        options = VariableTracker.propagate(self, args, kwargs.values())
        return variables.UserFunctionVariable(
            self.fn_cls.forward, **options
        ).call_function(tx, args, kwargs)


class BlackHoleVariable(VariableTracker):
    """A autograd.function context that just ignores everything (for forward extraction)"""

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        assert name in ("__setattr__", "save_for_backward"), name
        return variables.ConstantVariable(
            None, **VariableTracker.propagate(self, args, kwargs.values())
        )


class LambdaVariable(VariableTracker):
    def __init__(self, fn, **kwargs):
        super(LambdaVariable, self).__init__(**kwargs)
        self.fn = fn

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        return self.fn(*args, **kwargs).add_options(self)


class GetAttrVariable(VariableTracker):
    def __init__(self, obj, name, **kwargs):
        super(GetAttrVariable, self).__init__(**kwargs)
        assert isinstance(obj, VariableTracker)
        assert isinstance(name, str)
        self.obj = obj
        self.name = name

    def __str__(self):
        return f"{self.__class__.__name__}({self.obj}, {self.name})"

    def as_proxy(self):
        return getattr(self.obj.as_proxy(), self.name)

    def const_getattr(self, tx, name):
        if not isinstance(self.obj, variables.NNModuleVariable):
            raise NotImplementedError()
        step1 = tx.output.get_submodule(self.obj.module_key)
        if self.name not in step1.__dict__:
            raise NotImplementedError()
        step2 = inspect.getattr_static(step1, self.name)
        if name not in step2.__dict__:
            raise NotImplementedError()
        return inspect.getattr_static(step2, name)

    def reconstruct(self, codegen):
        codegen(self.obj)
        return codegen.create_load_attrs(self.name)

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        if isinstance(self.obj, AutogradFunctionVariable) and self.name == "apply":
            return self.obj.call_apply(tx, args, kwargs).add_options(self)
        return self.obj.call_method(tx, self.name, args, kwargs).add_options(self)

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        if (
            name == "__len__"
            and isinstance(self.obj, InspectSignatureVariable)
            and self.name == "parameters"
        ):
            return variables.ConstantVariable(
                self.obj.inspected.num_parameters(),
                **VariableTracker.propagate(self, self.obj, self.obj.inspected),
            )
        return super(GetAttrVariable, self).call_method(tx, name, args, kwargs)


class PythonModuleVariable(VariableTracker):
    def __init__(self, value: types.ModuleType, **kwargs):
        super(PythonModuleVariable, self).__init__(**kwargs)
        self.value = value

    def python_type(self):
        return types.ModuleType


class SkipFilesVariable(VariableTracker):
    def __init__(self, value, **kwargs):
        super().__init__(**kwargs)
        self.value = value

    def python_type(self):
        return type(self.value)

    def as_python_constant(self):
        return self.value

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        if inspect.getattr_static(self.value, "_torchdynamo_disable", False):
            unimplemented("call torchdynamo.disable() wrapped function")
        else:
            unimplemented("call_function in skip_files " + inspect.getfile(self.value))


class NumpyVariable(VariableTracker):
    """
    Wrapper around `numpy.*` for better error messages.
    """

    def __init__(self, value, **kwargs):
        super().__init__(**kwargs)
        self.value = value

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        unimplemented("numpy")

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        unimplemented("numpy")
