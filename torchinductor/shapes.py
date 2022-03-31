import collections
from typing import Dict

from sympy import Expr
from sympy import Integer
from sympy import Symbol


class SizeVarAllocator(object):
    def __init__(self, prefix="s", zero_one_const=True):
        super().__init__()
        self.prefix = prefix
        self.val_to_var: Dict[int, Expr] = {0: Integer(0), 1: Integer(1)}
        self.var_to_val: Dict[Expr, int] = collections.OrderedDict()
        if not zero_one_const:
            self.val_to_var.clear()

    def __getitem__(self, val):
        if val in self.val_to_var:
            return self.val_to_var[val]
        var = Symbol(f"{self.prefix}{len(self.var_to_val)}")
        self.val_to_var[val] = var
        self.var_to_val[var] = val
        return var

    def codegen(self, code, graph_inputs):
        """Assign all symbolic shapes to locals"""
        needed = set(map(str, self.var_to_val.keys()))
        for name, value in graph_inputs.items():
            shapes = value.get_size()
            for dim, shape in enumerate(shapes):
                shape = str(shape)
                if shape in needed:
                    needed.remove(shape)
                    code.writeline(f"{shape} = {name}.size({dim})")
        for name, value in graph_inputs.items():
            shapes = value.get_stride()
            for dim, shape in enumerate(shapes):
                shape = str(shape)
                if shape in needed:
                    needed.remove(shape)
                    code.writeline(f"{shape} = {name}.stride({dim})")
        assert not needed
