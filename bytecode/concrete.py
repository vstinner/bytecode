import dis
import inspect
import opcode as _opcode
import struct
import sys
import types

# alias to keep the 'bytecode' variable free
import bytecode as _bytecode
from bytecode.instr import (UNSET, Instr, Label, SetLineno,
                            FreeVar, CellVar, Compare,
                            const_key, _check_arg_int)

_WORDCODE = (sys.version_info >= (3, 6))


def _set_docstring(code, consts):
    if not consts:
        return
    first_const = consts[0]
    if isinstance(first_const, str) or first_const is None:
        code.docstring = first_const


class ConcreteInstr(Instr):
    """Concrete instruction.

    arg must be an integer in the range 0..2147483647.

    It has a read-only size attribute.
    """

    __slots__ = ('_size',)

    def __init__(self, name, arg=UNSET, *, lineno=None):
        self._set(name, arg, lineno)

    def _check_arg(self, name, opcode, arg):
        if opcode >= _opcode.HAVE_ARGUMENT:
            if arg is UNSET:
                raise ValueError("operation %s requires an argument" % name)

            _check_arg_int(name, arg)
        else:
            if arg is not UNSET:
                raise ValueError("operation %s has no argument" % name)

    def _set(self, name, arg, lineno):
        super()._set(name, arg, lineno)
        if _WORDCODE:
            size = 2
            if arg is not UNSET:
                while arg > 0xff:
                    size += 2
                    arg >>= 8
        else:
            size = 1
            if arg is not UNSET:
                size += 2
                if arg > 0xffff:
                    size += 3
        self._size = size

    @property
    def size(self):
        return self._size

    def _cmp_key(self, labels=None):
        return (self._lineno, self._name, self._arg)

    def get_jump_target(self, instr_offset):
        if self._opcode in _opcode.hasjrel:
            return instr_offset + self._size + self._arg
        if self._opcode in _opcode.hasjabs:
            return self._arg
        return None

    if _WORDCODE:
        def assemble(self):
            if self._arg is UNSET:
                return bytes((self._opcode, 0))

            arg = self._arg
            b = [self._opcode, arg & 0xff]
            while arg > 0xff:
                arg >>= 8
                b[:0] = [_opcode.EXTENDED_ARG, arg & 0xff]

            return bytes(b)
    else:
        def assemble(self):
            if self._arg is UNSET:
                return struct.pack('<B', self._opcode)

            arg = self._arg
            if arg > 0xffff:
                return struct.pack('<BHBH',
                                   _opcode.EXTENDED_ARG, arg >> 16,
                                   self._opcode, arg & 0xffff)
            else:
                return struct.pack('<BH', self._opcode, arg)

    @classmethod
    def disassemble(cls, lineno, code, offset):
        op = code[offset]
        if op >= _opcode.HAVE_ARGUMENT:
            if _WORDCODE:
                arg = code[offset + 1]
            else:
                arg = code[offset + 1] + code[offset + 2] * 256
        else:
            arg = UNSET
        name = _opcode.opname[op]
        return cls(name, arg, lineno=lineno)


class ConcreteBytecode(_bytecode.BaseBytecode, list):

    def __init__(self):
        super().__init__()
        self.consts = []
        self.names = []
        self.varnames = []

    def __iter__(self):
        instructions = super().__iter__()
        for instr in instructions:
            if not isinstance(instr, (ConcreteInstr, SetLineno)):
                raise ValueError("ConcreteBytecode must only contain "
                                 "ConcreteInstr and SetLineno objects, "
                                 "but %s was found"
                                 % instr.__class__.__name__)

            yield instr

    def __repr__(self):
        return '<ConcreteBytecode instr#=%s>' % len(self)

    def __eq__(self, other):
        if type(self) != type(other):
            return False

        const_keys1 = list(map(const_key, self.consts))
        const_keys2 = list(map(const_key, other.consts))
        if const_keys1 != const_keys2:
            return False

        if self.names != other.names:
            return False
        if self.varnames != other.varnames:
            return False

        return super().__eq__(other)

    @staticmethod
    def from_code(code, *, extended_arg=False):
        line_starts = dict(dis.findlinestarts(code))

        # find block starts
        instructions = []
        offset = 0
        lineno = code.co_firstlineno
        while offset < len(code.co_code):
            if offset in line_starts:
                lineno = line_starts[offset]

            instr = ConcreteInstr.disassemble(lineno, code.co_code, offset)

            instructions.append(instr)
            offset += instr.size

        # replace jump targets with blocks
        if not extended_arg:
            extended_arg = None
            index = 0
            while index < len(instructions):
                instr = instructions[index]

                if instr.name == 'EXTENDED_ARG':
                    if extended_arg is not None:
                        if not _WORDCODE:
                            raise ValueError("EXTENDED_ARG followed "
                                             "by EXTENDED_ARG")
                        extended_arg = (extended_arg << 8) + instr.arg
                    else:
                        extended_arg = instr.arg
                    del instructions[index]
                    continue

                if extended_arg is not None:
                    if _WORDCODE:
                        arg = (extended_arg << 8) + instr.arg
                    else:
                        arg = (extended_arg << 16) + instr.arg
                    extended_arg = None

                    instr = ConcreteInstr(instr.name, arg, lineno=instr.lineno)
                    instructions[index] = instr

                index += 1

            if extended_arg is not None:
                raise ValueError("EXTENDED_ARG at the end of the code")

        bytecode = ConcreteBytecode()
        bytecode.name = code.co_name
        bytecode.filename = code.co_filename
        bytecode.flags = code.co_flags
        bytecode.argcount = code.co_argcount
        bytecode.kwonlyargcount = code.co_kwonlyargcount
        bytecode.first_lineno = code.co_firstlineno
        bytecode.names = list(code.co_names)
        bytecode.consts = list(code.co_consts)
        bytecode.varnames = list(code.co_varnames)
        bytecode.freevars = list(code.co_freevars)
        bytecode.cellvars = list(code.co_cellvars)
        _set_docstring(bytecode, code.co_consts)

        bytecode[:] = instructions
        return bytecode

    def _normalize_lineno(self):
        lineno = self.first_lineno
        for instr in self:
            # if instr.lineno is not set, it's inherited from the previous
            # instruction, or from self.first_lineno
            if instr.lineno is not None:
                lineno = instr.lineno

            if isinstance(instr, ConcreteInstr):
                yield (lineno, instr)

    def _assemble_code(self):
        offset = 0
        code_str = []
        linenos = []
        for lineno, instr in self._normalize_lineno():
            code_str.append(instr.assemble())
            linenos.append((offset, lineno))
            offset += instr.size
        code_str = b''.join(code_str)
        return (code_str, linenos)

    @staticmethod
    def _assemble_lnotab(first_lineno, linenos):
        lnotab = []
        old_offset = 0
        old_lineno = first_lineno
        for offset, lineno in linenos:
            dlineno = lineno - old_lineno
            if dlineno == 0:
                continue
            # FIXME: be kind, force monotonic line numbers? add an option?
            if dlineno < 0 and sys.version_info < (3, 6):
                raise ValueError("negative line number delta is not supported "
                                 "on Python < 3.6")
            old_lineno = lineno

            doff = offset - old_offset
            old_offset = offset

            while doff > 255:
                lnotab.append(b'\xff\x00')
                doff -= 255

            while dlineno < -127:
                lnotab.append(struct.pack('Bb', 0, -127))
                dlineno -= -127

            while dlineno > 126:
                lnotab.append(struct.pack('Bb', 0, 126))
                dlineno -= 126

            assert 0 <= doff <= 255
            assert -127 <= dlineno <= 126

            lnotab.append(struct.pack('Bb', doff, dlineno))

        return b''.join(lnotab)

    def compute_stacksize(self):
        bytecode = self.to_bytecode()
        cfg = _bytecode.ControlFlowGraph.from_bytecode(bytecode)
        return cfg.compute_stacksize()

    def to_code(self):
        code_str, linenos = self._assemble_code()
        lnotab = self._assemble_lnotab(self.first_lineno, linenos)
        nlocals = len(self.varnames)
        stacksize = self.compute_stacksize()
        return types.CodeType(self.argcount,
                              self.kwonlyargcount,
                              nlocals,
                              stacksize,
                              self.flags,
                              code_str,
                              tuple(self.consts),
                              tuple(self.names),
                              tuple(self.varnames),
                              self.filename,
                              self.name,
                              self.first_lineno,
                              lnotab,
                              tuple(self.freevars),
                              tuple(self.cellvars))

    def to_bytecode(self):
        # find jump targets
        jump_targets = set()
        offset = 0
        for instr in self:
            if isinstance(instr, SetLineno):
                continue
            target = instr.get_jump_target(offset)
            if target is not None:
                jump_targets.add(target)
            offset += instr.size

        # create labels
        jumps = []
        instructions = []
        labels = {}
        offset = 0
        ncells = len(self.cellvars)

        for lineno, instr in self._normalize_lineno():
            if offset in jump_targets:
                label = Label()
                labels[offset] = label
                instructions.append(label)

            jump_target = instr.get_jump_target(offset)
            size = instr.size

            arg = instr.arg
            # FIXME: better error reporting
            if instr.opcode in _opcode.hasconst:
                arg = self.consts[arg]
            elif instr.opcode in _opcode.haslocal:
                arg = self.varnames[arg]
            elif instr.opcode in _opcode.hasname:
                arg = self.names[arg]
            elif instr.opcode in _opcode.hasfree:
                if arg < ncells:
                    name = self.cellvars[arg]
                    arg = CellVar(name)
                else:
                    name = self.freevars[arg - ncells]
                    arg = FreeVar(name)
            elif instr.opcode in _opcode.hascompare:
                arg = Compare(arg)

            if jump_target is None:
                instr = Instr(instr.name, arg, lineno=lineno)
            else:
                instr_index = len(instructions)
            instructions.append(instr)
            offset += size

            if jump_target is not None:
                jumps.append((instr_index, jump_target))

        # replace jump targets with labels
        for index, jump_target in jumps:
            instr = instructions[index]
            # FIXME: better error reporting on missing label
            label = labels[jump_target]
            instructions[index] = Instr(instr.name, label, lineno=instr.lineno)

        bytecode = _bytecode.Bytecode()
        bytecode._copy_attr_from(self)

        nargs = bytecode.argcount + bytecode.kwonlyargcount
        if bytecode.flags & inspect.CO_VARARGS:
            nargs += 1
        if bytecode.flags & inspect.CO_VARKEYWORDS:
            nargs += 1
        bytecode.argnames = self.varnames[:nargs]
        _set_docstring(bytecode, self.consts)

        bytecode.extend(instructions)
        return bytecode


class _ConvertBytecodeToConcrete:

    def __init__(self, code):
        assert isinstance(code, _bytecode.Bytecode)
        self.bytecode = code

        # temporary variables
        self.instructions = []
        self.jumps = []
        self.labels = {}

        # used to build ConcreteBytecode() object
        self.consts = {}
        self.names = []
        self.varnames = []

    def add_const(self, value):
        key = const_key(value)
        if key in self.consts:
            return self.consts[key]
        index = len(self.consts)
        self.consts[key] = index
        return index

    @staticmethod
    def add(names, name):
        try:
            index = names.index(name)
        except ValueError:
            index = len(names)
            names.append(name)
        return index

    def concrete_instructions(self):
        ncells = len(self.bytecode.cellvars)
        lineno = self.bytecode.first_lineno

        for instr in self.bytecode:
            if isinstance(instr, Label):
                self.labels[instr] = len(self.instructions)
                continue

            if isinstance(instr, SetLineno):
                lineno = instr.lineno
                continue

            if isinstance(instr, ConcreteInstr):
                instr = instr.copy()
            else:
                assert isinstance(instr, Instr)

                if instr.lineno is not None:
                    lineno = instr.lineno

                arg = instr.arg
                is_jump = isinstance(arg, Label)
                if is_jump:
                    label = arg
                    # fake value, real value is set in compute_jumps()
                    arg = 0
                elif instr.opcode in _opcode.hasconst:
                    arg = self.add_const(arg)
                elif instr.opcode in _opcode.haslocal:
                    arg = self.add(self.varnames, arg)
                elif instr.opcode in _opcode.hasname:
                    arg = self.add(self.names, arg)
                elif instr.opcode in _opcode.hasfree:
                    if isinstance(arg, CellVar):
                        arg = self.bytecode.cellvars.index(arg.name)
                    else:
                        assert isinstance(arg, FreeVar)
                        arg = ncells + self.bytecode.freevars.index(arg.name)
                elif instr.opcode in _opcode.hascompare:
                    if isinstance(arg, Compare):
                        arg = arg.value

                instr = ConcreteInstr(instr.name, arg, lineno=lineno)
                if is_jump:
                    self.jumps.append((len(self.instructions), label, instr))

            self.instructions.append(instr)

    def compute_jumps(self):
        offsets = []
        offset = 0
        for index, instr in enumerate(self.instructions):
            offsets.append(offset)
            offset += instr.size
        # needed if a label is at the end
        offsets.append(offset)

        # fix argument of jump instructions: resolve labels
        modified = False
        for index, label, instr in self.jumps:
            target_index = self.labels[label]
            target_offset = offsets[target_index]

            if instr.opcode in _opcode.hasjrel:
                instr_offset = offsets[index]
                target_offset -= (instr_offset + instr.size)

            old_size = instr.size
            # FIXME: better error report if target_offset is negative
            instr.arg = target_offset
            if instr.size != old_size:
                modified = True

        return modified

    def to_concrete_bytecode(self):
        first_const = self.bytecode.docstring
        if first_const is not UNSET:
            self.add_const(first_const)

        self.varnames.extend(self.bytecode.argnames)

        self.concrete_instructions()
        modified = self.compute_jumps()
        if modified:
            modified = self.compute_jumps()
            if modified:
                raise RuntimeError("compute_jumps() must not modify jumps "
                                   "at the second iteration")

        consts = [None] * len(self.consts)
        for item, index in self.consts.items():
            # const_key(value)[1] is value: see const_key() function
            consts[index] = item[1]

        concrete = ConcreteBytecode()
        concrete._copy_attr_from(self.bytecode)
        concrete.consts = consts
        concrete.names = self.names
        concrete.varnames = self.varnames

        # copy instructions
        concrete[:] = self.instructions
        return concrete
