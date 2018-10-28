import openpyxl.formula.tokenizer as tokenizer
from networkx.classes.digraph import DiGraph

from pycel.excelutil import (
    get_linest_degree,
    is_range,
    resolve_range,
    split_address,
    split_range,
)


class ParserError(Exception):
    """"Base class for Parser errors"""


class Tokenizer(tokenizer.Tokenizer):
    """Amend openpyxl tokenizer"""

    def __init__(self, formula):
        super(Tokenizer, self).__init__(formula)
        self.items = self._items()

    def _items(self):
        """ convert to use our Token"""
        t = [None] + [Token.from_token(t) for t in self.items] + [None]

        # convert or remove unneeded whitespace
        tokens = []
        for prev_token, token, next_token in zip(t, t[1:], t[2:]):
            if token.type != Token.WSPACE or not prev_token or not next_token:
                tokens.append(token)

            elif (prev_token.matches(type_=Token.FUNC, subtype=Token.CLOSE) or
                  prev_token.matches(type_=Token.PAREN, subtype=Token.CLOSE) or
                  prev_token.type == Token.OPERAND
            ) and (
                next_token.matches(type_=Token.FUNC, subtype=Token.OPEN) or
                next_token.matches(type_=Token.PAREN, subtype=Token.OPEN) or
                next_token.type == Token.OPERAND
            ):
                # this whitespace is an intersect operator
                tokens.append(Token(token.value, Token.OP_IN, Token.INTERSECT))
        return tokens


class Token(tokenizer.Token):
    """Amend openpyxl token"""

    INTERSECT = "INTERSECT"
    ARRAYROW = "ARRAYROW"

    @classmethod
    def from_token(cls, token, value=None, type_=None, subtype=None):
        return cls(
            token.value if value is None else value,
            token.type if type_ is None else type_,
            token.subtype if subtype is None else subtype
        )

    @property
    def is_operator(self):
        return self.type in (Token.OP_PRE, Token.OP_IN, Token.OP_POST)

    @property
    def is_funcopen(self):
        return self.subtype == Token.OPEN and self.type in (
            Token.FUNC, Token.ARRAY, Token.ARRAYROW)

    def matches(self, type_=None, subtype=None, value=None):
        return ((type_ is None or self.type == type_) and
                (subtype is None or self.subtype == subtype) and
                (value is None or self.value == value))


class ASTNode(object):
    """A generic node in the AST used to compile a cell's formula"""

    def __init__(self, token):
        super(ASTNode, self).__init__()
        self.token = token
        self._ast = None
        self._parent = None
        self._children = None
        self._descendants = None

    @classmethod
    def create(cls, token):
        """Simple factory function"""
        if token.type == Token.OPERAND:
            if token.subtype == Token.RANGE:
                return RangeNode(token)
            else:
                return OperandNode(token)

        elif token.is_funcopen:
            return FunctionNode(token)

        elif token.is_operator:
            return OperatorNode(token)

        raise ParserError('Unknown token type: {}'.format(repr(token)))

    def __str__(self):
        return str(self.token.value.strip('('))

    def __repr__(self):
        return '{}<{}>'.format(type(self).__name__,
                               str(self.token.value.strip('(')))

    @property
    def ast(self):
        return self._ast

    @ast.setter
    def ast(self, value):
        self._ast = value

    @property
    def value(self):
        return self.token.value

    @property
    def type(self):
        return self.token.type

    @property
    def subtype(self):
        return self.token.subtype

    @property
    def children(self):
        if self._children is None:
            args = self.ast.predecessors(self)
            self._children = sorted(args, key=lambda x: self.ast.node[x]['pos'])
        # args.reverse()
        return self._children

    @property
    def descendants(self):
        if self._descendants is None:
            self._descendants = list(
                n for n in self.ast.nodes(self) if n[0] != self)
        return self._descendants

    @property
    def parent(self):
        if self._parent is None:
            self._parent = next(self.ast.successors(self), None)
        return self._parent

    def emit(self, context=None):
        """Emit code"""
        return self.value


class OperatorNode(ASTNode):
    # convert the operator to python equivalents
    op_map = {
        "^": "**",
        "=": "==",
        "&": "+",
        " ": "+"  # range intersection
    }

    def emit(self, context=None):
        xop = self.value

        # Get the arguments
        args = self.children

        op = self.op_map.get(xop, xop)

        if self.type == Token.OP_PRE:
            return "-" + args[0].emit(context)

        parent = self.parent
        # dont render the ^{1,2,..} part in a linest formula
        # TODO: bit of a hack
        if op == "**":
            if parent and parent.value.lower() == "linest(":
                return args[0].emit(context)

        # TODO silly hack to work around the fact that None < 0 is True
        #  (happens on blank cells)
        if op.startswith('<'):
            aa = args[0].emit(context)
            if not args[0].token.matches(
                    type_=Token.OPERAND, subtype=Token.NUMBER):
                aa = "({} if {} is not None else 0)".format(aa, aa)
            ss = "{} {} {}".format(aa, op, args[1].emit(context))

        elif op.startswith('>'):
            aa = args[1].emit(context)
            if not args[1].token.matches(
                    type_=Token.OPERAND, subtype=Token.NUMBER):
                aa = "({} if {} is not None else 0)".format(aa, aa)
            ss = "{} {} {}".format(args[0].emit(context), op, aa)
        else:
            if op != ',':
                op = ' ' + op
            ss = '{}{} {}'.format(
                args[0].emit(context), op, args[1].emit(context))

        # avoid needless parentheses
        if parent and not isinstance(parent, FunctionNode):
            ss = "(" + ss + ")"

        return ss


class OperandNode(ASTNode):

    def emit(self, context=None):
        if self.subtype == self.token.LOGICAL:
            return str(self.value.lower() == "true")

        elif self.subtype in ("TEXT", "ERROR") and len(self.value) > 2:
            # if the string contains quotes, escape them
            return '"{}"'.format(self.value.replace('""', '\\"').strip('"'))

        else:
            return self.value


class RangeNode(OperandNode):
    """Represents a spreadsheet cell or range, e.g., A5 or B3:C20"""

    def get_cells(self):
        return resolve_range(self.value)[0]

    def emit(self, context=None):
        # resolve the range into cells
        rng = self.value.replace('$', '')
        sheet = context.curcell.sheet + "!" if context else ""
        if is_range(rng):
            sh, start, end = split_range(rng)
            if sh:
                return 'eval_range("' + rng + '")'
            else:
                return 'eval_range("' + sheet + rng + '")'
        else:
            sh, col, row = split_address(rng)
            if sh:
                return 'eval_cell("' + rng + '")'
            else:
                return 'eval_cell("' + sheet + rng + '")'


class FunctionNode(ASTNode):
    """AST node representing a function call"""

    """
    A dictionary that maps excel function names onto python equivalents. You
    should only add an entry to this map if the python name is different from
    the excel name (which it may need to be to prevent conflicts with
    existing python functions with that name, e.g., max).

    So if excel defines a function foobar(), all you have to do is add a
    function called foobar to this module.  You only need to add it to the
    function map, if you want to use a different name in the python code.

    Note: some functions (if, pi, atan2, and, or, array, ...) are already taken
    care of in the FunctionNode code, so adding them here will have no effect.
    """

    # dict of excel equivalent functions
    func_map = {
        "ln": "xlog",
        "min": "xmin",
        "max": "xmax",
        "sum": "xsum",
        "gammaln": "lgamma",
        "round": "xround"
    }

    def __init__(self, *args):
        super(FunctionNode, self).__init__(*args)
        self.num_args = 0

    def comma_join_emit(self, context=None, fmt_str=None):
        if fmt_str is None:
            return ", ".join(n.emit(context) for n in self.children)
        else:
            return ", ".join(
                fmt_str.format(n.emit(context)) for n in self.children)

    def emit(self, context=None):
        func = self.value.lower().strip('(')

        # if a special handler is needed
        handler = getattr(self, 'func_{}'.format(func), None)
        if handler is not None:
            return handler(context)

        else:
            # map to the correct name
            return "{}({})".format(
                self.func_map.get(func, func), self.comma_join_emit(context))

    def func_atan2(self, context=None):
        # swap arguments
        a1, a2 = (a.emit(context) for a in self.children)
        return "atan2({}, {})".format(a2, a1)

    @staticmethod
    def func_pi(context=None):
        # constant, no parens
        return "pi"

    def func_if(self, context=None):
        # inline the if
        args = [c.emit(context) for c in self.children] + [0]
        if len(args) in (3, 4):
            return "({} if {} else {})".format(args[1], args[0], args[2])

        raise ParserError(
            "IF with %s arguments not supported".format(len(args) - 1))

    def func_array(self, context=None):
        if len(self.children) == 1:
            return '[{}]'.format(self.children[0].emit(context))
        else:
            # multiple rows
            return '[{}]'.format(self.comma_join_emit(context, '[{}]'))

    def func_arrayrow(self, context=None):
        # simply create a list
        return self.comma_join_emit(context)

    def func_linest(self, context=None):
        func = self.value.lower().strip('(')
        code = '{}({}'.format(func, self.comma_join_emit(context))

        if not context:
            degree, coef = -1, -1
        else:
            # linests are often used as part of an array formula spanning
            # multiple cells, one cell for each coefficient.  We have to
            # figure out where we currently are in that range.
            degree, coef = get_linest_degree(context.excel, context.curcell)

        # if we are the only linest (degree is one) and linest is nested
        # return vector, else return the coef.
        if func == "linest":
            code += ", degree=%s)" % degree
        else:
            code += ")"

        if not (degree == 1 and self.parent):
            code += "[%s]" % (coef - 1)

        return code

    func_linestmario = func_linest

    def func_and(self, context=None):
        return "all([{}])".format(self.comma_join_emit(context))

    def func_or(self, context=None):
        return "any([{}])".format(self.comma_join_emit(context))


class Operator:
    """Small wrapper class to manage operators during shunting yard"""

    def __init__(self, value, precedence, associativity):
        self.value = value
        self.precedence = precedence
        self.associativity = associativity

    def __lt__(self, other):
        return (self.precedence < other.precedence or
                self.associativity == "left" and
                self.precedence == other.precedence
                )


class ExcelParser(object):
    """Take an Excel function and compile it to Python code."""

    operators = {
        # http://office.microsoft.com/en-us/excel-help/
        #   calculation-operators-and-precedence-HP010078886.aspx
        ':': Operator(':', 8, 'left'),
        ' ': Operator(' ', 8, 'left'),  # range intersection
        ',': Operator(',', 8, 'left'),
        'u-': Operator('u-', 7, 'left'),  # unary negation
        '%': Operator('%', 6, 'left'),
        '^': Operator('^', 5, 'left'),
        '*': Operator('*', 4, 'left'),
        '/': Operator('/', 4, 'left'),
        '+': Operator('+', 3, 'left'),
        '-': Operator('-', 3, 'left'),
        '&': Operator('&', 2, 'left'),
        '=': Operator('=', 1, 'left'),
        '<': Operator('<', 1, 'left'),
        '>': Operator('>', 1, 'left'),
        '<=': Operator('<=', 1, 'left'),
        '>=': Operator('>=', 1, 'left'),
        '<>': Operator('<>', 1, 'left'),
    }

    @classmethod
    def parse_to_rpn(cls, expression):
        """
        Parse an excel formula expression into reverse polish notation

        Core algorithm taken from wikipedia with varargs extensions from
        http://www.kallisti.net.nz/blog/2008/02/extension-to-the-shunting-yard-
            algorithm-to-allow-variable-numbers-of-arguments-to-functions/
        """

        lexer = Tokenizer(expression)

        # amend token stream to ease code production
        tokens = []
        for token, next_token in zip(lexer.items, lexer.items[1:] + [None]):

            if token.matches(Token.FUNC, Token.OPEN):
                tokens.append(token)
                token = Token('(', Token.PAREN, Token.OPEN)

            elif token.matches(Token.FUNC, Token.CLOSE):
                token = Token(')', Token.PAREN, Token.CLOSE)

            elif token.matches(Token.ARRAY, Token.OPEN):
                tokens.append(token)
                tokens.append(Token('(', Token.PAREN, Token.OPEN))
                tokens.append(Token('', Token.ARRAYROW, Token.OPEN))
                token = Token('(', Token.PAREN, Token.OPEN)

            elif token.matches(Token.ARRAY, Token.CLOSE):
                tokens.append(token)
                token = Token(')', Token.PAREN, Token.CLOSE)

            elif token.matches(Token.SEP, Token.ROW):
                tokens.append(Token(')', Token.PAREN, Token.CLOSE))
                tokens.append(Token(',', Token.SEP, Token.ARG))
                tokens.append(Token('', Token.ARRAYROW, Token.OPEN))
                token = Token('(', Token.PAREN, Token.OPEN)

            elif token.matches(Token.PAREN, Token.OPEN):
                token.value = '('

            elif token.matches(Token.PAREN, Token.CLOSE):
                token.value = ')'

            if token:
                tokens.append(token)

        output = []
        stack = []
        were_values = []
        arg_count = []

        for token in tokens:
            if token.type == token.OPERAND:

                output.append(ASTNode.create(token))
                if were_values:
                    were_values[-1] = True

            elif token.type != token.PAREN and token.subtype == token.OPEN:

                if token.type in (token.ARRAY, Token.ARRAYROW):
                    token = Token(token.type, token.type, token.subtype)

                stack.append(token)
                arg_count.append(0)
                if were_values:
                    were_values[-1] = True
                were_values.append(False)

            elif token.type == token.SEP:

                while stack and (stack[-1].subtype != token.OPEN):
                    output.append(ASTNode.create(stack.pop()))

                if not len(were_values):
                    raise ParserError("Mismatched or misplaced parentheses")

                if were_values.pop():
                    arg_count[-1] += 1
                were_values.append(False)

            elif token.is_operator:

                if token.type == token.OP_PRE and token.value == "-":
                    o1 = cls.operators['u-']
                else:
                    o1 = cls.operators[token.value]

                while stack and stack[-1].is_operator:

                    if stack[-1].matches(type_=Token.OP_PRE, value='-'):
                        o2 = cls.operators['u-']
                    else:
                        o2 = cls.operators[stack[-1].value]

                    if o1 < o2:
                        output.append(ASTNode.create(stack.pop()))
                    else:
                        break

                stack.append(token)

            elif token.subtype == token.OPEN:
                assert token.type in (token.FUNC, token.PAREN, token.ARRAY)
                stack.append(token)

            elif token.subtype == token.CLOSE:

                while stack and stack[-1].subtype != Token.OPEN:
                    output.append(ASTNode.create(stack.pop()))

                if not stack:
                    raise ParserError("Mismatched or misplaced parentheses")

                stack.pop()

                if stack and stack[-1].is_funcopen:
                    f = ASTNode.create(stack.pop())
                    f.num_args = arg_count.pop() + int(were_values.pop())
                    output.append(f)

        while stack:
            if stack[-1].subtype in (Token.OPEN, Token.CLOSE):
                raise ParserError("Mismatched or misplaced parentheses")

            output.append(ASTNode.create(stack.pop()))

        return output

    @classmethod
    def build_ast(cls, rpn_expression):
        """build an AST from an Excel formula

        :param rpn_expression: a string formula or the after parse_to_rpn()
        :return: AST which can be used to generate code
        """

        if not isinstance(rpn_expression, list):
            rpn_expression = cls.parse_to_rpn(rpn_expression)

        # use a directed graph to store the syntax tree
        ast = DiGraph()

        # production stack
        stack = []

        for node in rpn_expression:
            # The graph does not maintain the order of adding nodes/edges, so
            # add an attribute 'pos' so we can always sort to the correct order

            node.ast = ast
            if isinstance(node, OperatorNode):
                if node.token.type == node.token.OP_IN:
                    try:
                        arg2 = stack.pop()
                        arg1 = stack.pop()
                    except IndexError:
                        raise ParserError(
                            "'{}' operator missing operand".format(
                                node.token.value))
                    ast.add_node(arg1, pos=0)
                    ast.add_node(arg2, pos=1)
                    ast.add_edge(arg1, node)
                    ast.add_edge(arg2, node)
                else:
                    try:
                        arg1 = stack.pop()
                    except IndexError:
                        raise ParserError(
                            "'{}' operator missing operand".format(
                                node.token.value))
                    ast.add_node(arg1, pos=1)
                    ast.add_edge(arg1, node)

            elif isinstance(node, FunctionNode):
                if node.num_args:
                    args = stack[-node.num_args:]
                    del stack[-node.num_args:]
                    for i, a in enumerate(args):
                        ast.add_node(a, pos=i)
                        ast.add_edge(a, node)
            else:
                ast.add_node(node, pos=0)

            stack.append(node)

        assert 1 == len(stack)
        return stack[0]
