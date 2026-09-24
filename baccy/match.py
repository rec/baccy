import ast
from collections.abc import Mapping

from simpleeval import SimpleEval

_NAMES = {
    'duration',
    'main',
    'device',
    'channels',
    'track',
    'format',
    'player',
    'has_player',
}
_MAX_LENGTH = 512
_MAX_STRING = 256
_MAX_ITEMS = 32
_MAX_NODES = 96
_MAX_DEPTH = 12


class MatchExpression:
    def __init__(self, text: str) -> None:
        if len(text) > _MAX_LENGTH:
            raise ValueError(f'match expression exceeds {_MAX_LENGTH} characters')
        try:
            self.tree = ast.parse(text, mode='eval')
        except SyntaxError as error:
            raise ValueError(f'invalid match expression: {error.msg}') from error
        _validate(self.tree)

    def matches(self, values: Mapping[str, object]) -> bool:
        evaluator = SimpleEval(functions={}, names=dict(values), allowed_attrs={})
        if evaluator.nodes is None:
            raise RuntimeError('simpleeval did not initialize AST handlers')
        evaluator.nodes[ast.List] = lambda node: [
            evaluator._eval(item) for item in node.elts
        ]
        evaluator.nodes[ast.Tuple] = lambda node: tuple(
            evaluator._eval(item) for item in node.elts
        )
        return bool(evaluator.eval('', previously_parsed=self.tree.body))


def validate_match(text: str) -> str:
    MatchExpression(text)
    return text


def _validate(node: ast.AST) -> None:
    nodes = list(ast.walk(node))
    if len(nodes) > _MAX_NODES:
        raise ValueError(f'match expression exceeds {_MAX_NODES} AST nodes')
    _validate_node(node, 0)


def _validate_node(node: ast.AST, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError(f'match expression exceeds {_MAX_DEPTH} nesting levels')
    if isinstance(node, ast.Expression):
        _validate_node(node.body, depth + 1)
        return
    if isinstance(node, ast.Name):
        if node.id not in _NAMES:
            raise ValueError(f'unknown match name: {node.id}')
        return
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, str | int | float | bool | type(None)):
            raise ValueError('invalid match literal')
        if isinstance(node.value, str) and len(node.value) > _MAX_STRING:
            raise ValueError(f'match string literal exceeds {_MAX_STRING} characters')
        return
    if isinstance(node, ast.List | ast.Tuple):
        if len(node.elts) > _MAX_ITEMS:
            raise ValueError(f'match literal exceeds {_MAX_ITEMS} items')
        for item in node.elts:
            _validate_node(item, depth + 1)
        return
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        for item in node.values:
            _validate_node(item, depth + 1)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _validate_node(node.operand, depth + 1)
        return
    comparison = (
        ast.Eq | ast.NotEq | ast.Lt | ast.LtE | ast.Gt | ast.GtE | ast.In | ast.NotIn
    )
    if isinstance(node, ast.Compare) and all(
        isinstance(operator, comparison) for operator in node.ops
    ):
        _validate_node(node.left, depth + 1)
        for comparator in node.comparators:
            _validate_node(comparator, depth + 1)
        return
    raise ValueError(f'forbidden match expression syntax: {type(node).__name__}')
