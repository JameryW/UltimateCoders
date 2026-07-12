"""Step condition expression evaluator.

A tiny recursive-descent parser for conditional step execution. No
external dependencies — hand-written tokenizer + Pratt-style descent.

Grammar:
    expr       := or_expr
    or_expr    := and_expr ( "||" and_expr )*
    and_expr   := not_expr ( "&&" not_expr )*
    not_expr   := "!" not_expr | comparison
    comparison := primary ( ("==" | "!=") primary )?
    primary    := atom | "(" or_expr ")"
    atom       := "prev.success"
                 | "prev.files.contains(" STRING ")"
                 | "prev.summary.contains(" STRING ")"
                 | "true" | "false"

Semantics:
    - ``prev`` = the previous step's AgentOutput (None for step 0).
    - Step 0 (no prev): ``prev.success`` = False,
      ``prev.files.contains(x)`` = False, ``prev.summary.contains(x)`` = False.
    - ``==`` / ``!=`` only apply to ``prev.success`` vs ``true``/``false``.
    - Whitespace tolerant.
    - Empty/whitespace-only condition = always run (returns True, no eval).
    - Parse error → raise ConditionError (caller fails the subtask).
"""

from __future__ import annotations

from ultimate_coders.agent.sandbox import AgentOutput


class ConditionError(Exception):
    """Raised when a condition expression cannot be parsed/evaluated."""


# ── Tokenizer ────────────────────────────────────────────────────


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"Token({self.kind!r}, {self.value!r})"


# Token kinds. Multi-char operators must be tried before single-char.
_OPERATORS = ("||", "&&", "==", "!=", "!", "(", ")")
# Keywords/identifiers we recognize at the atom level.
_KEYWORDS = ("prev.success", "true", "false")


def _tokenize(text: str) -> list[_Token]:
    """Tokenize the condition expression.

    Produces tokens for: ||, &&, ==, !=, !, (, ), prev.success,
    prev.files.contains("..."), prev.summary.contains("..."), true, false.
    String literals inside contains() are captured as STRING tokens.
    """
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Skip whitespace.
        if ch.isspace():
            i += 1
            continue
        # Multi-char operators.
        matched = False
        for op in _OPERATORS:
            if text[i : i + len(op)] == op:
                tokens.append(_Token("OP", op))
                i += len(op)
                matched = True
                break
        if matched:
            continue
        # Parentheses (also in _OPERators as single-char, already handled
        # above — this is just a safety net for readability).
        if ch == "(" or ch == ")":
            tokens.append(_Token("OP", ch))
            i += 1
            continue
        # Identifiers / keywords: [a-zA-Z_][a-zA-Z0-9_.]*
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            word = text[i:j]
            i = j
            # Check for prev.files.contains("...") / prev.summary.contains("...")
            # The identifier reader consumed the whole "prev.files.contains"
            # because "." is in the identifier char set. Split it here.
            for prefix, kind in (
                ("prev.files.contains", "CONTAINS_FILES"),
                ("prev.summary.contains", "CONTAINS_SUMMARY"),
            ):
                if word == prefix:
                    # Read the rest of the text for the ("...") part.
                    rest = text[i:]
                    k = 0
                    # Skip whitespace.
                    while k < len(rest) and rest[k].isspace():
                        k += 1
                    # Expect "("
                    if k >= len(rest) or rest[k] != "(":
                        raise ConditionError(
                            f"Expected '(' after '{word}', got: {rest[k:k+20]!r}"
                        )
                    k += 1
                    # Skip whitespace.
                    while k < len(rest) and rest[k].isspace():
                        k += 1
                    # Expect a double-quoted string.
                    if k >= len(rest) or rest[k] != '"':
                        raise ConditionError(
                            f"Expected a double-quoted string in {word}(), got: {rest[k:k+20]!r}"
                        )
                    k += 1
                    str_start = k
                    # Read until closing quote (no escape handling — simple).
                    while k < len(rest) and rest[k] != '"':
                        k += 1
                    if k >= len(rest):
                        raise ConditionError(f"Unterminated string in {word}()")
                    str_val = rest[str_start:k]
                    k += 1  # skip closing quote
                    # Skip whitespace.
                    while k < len(rest) and rest[k].isspace():
                        k += 1
                    # Expect ")"
                    if k >= len(rest) or rest[k] != ")":
                        raise ConditionError(
                            f"Expected ')' to close {word}(), got: {rest[k:k+20]!r}"
                        )
                    k += 1
                    # Advance i past this whole construct.
                    i += k
                    tokens.append(_Token(kind, str_val))
                    break
            else:
                # Not a contains construct — check for simple keywords.
                if word in _KEYWORDS:
                    tokens.append(_Token("KW", word))
                    continue
                raise ConditionError(f"Unknown identifier: {word!r}")
            continue
        # Anything else is unexpected.
        raise ConditionError(f"Unexpected character: {ch!r} at position {i}")
    return tokens


# ── Parser (recursive descent) ───────────────────────────────────


class _Parser:
    """Recursive-descent parser. Produces a bool result directly.

    Since the grammar has no variables beyond ``prev`` (which is a fixed
    input), we evaluate as we parse — no AST intermediate needed.
    """

    def __init__(self, tokens: list[_Token], prev: AgentOutput | None) -> None:
        self.tokens = tokens
        self.pos = 0
        self.prev = prev

    @property
    def _cur(self) -> _Token | None:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _advance(self) -> _Token | None:
        tok = self._cur
        self.pos += 1
        return tok

    def _expect_op(self, op: str) -> None:
        tok = self._cur
        if tok is None or tok.kind != "OP" or tok.value != op:
            raise ConditionError(f"Expected '{op}', got {tok}")
        self._advance()

    def parse(self) -> bool:
        result = self._or_expr()
        if self._cur is not None:
            raise ConditionError(f"Unexpected trailing token: {self._cur}")
        return result

    def _or_expr(self) -> bool:
        result = self._and_expr()
        while self._cur is not None and self._cur.kind == "OP" and self._cur.value == "||":
            self._advance()
            right = self._and_expr()
            result = result or right
        return result

    def _and_expr(self) -> bool:
        result = self._not_expr()
        while self._cur is not None and self._cur.kind == "OP" and self._cur.value == "&&":
            self._advance()
            right = self._not_expr()
            result = result and right
        return result

    def _not_expr(self) -> bool:
        if self._cur is not None and self._cur.kind == "OP" and self._cur.value == "!":
            self._advance()
            return not self._not_expr()
        return self._comparison()

    def _comparison(self) -> bool:
        left = self._primary()
        # Check for == or !=
        if self._cur is not None and self._cur.kind == "OP" and self._cur.value in ("==", "!="):
            op = self._cur.value
            self._advance()
            right = self._primary()
            if op == "==":
                return left == right
            else:
                return left != right
        return left

    def _primary(self) -> bool:
        tok = self._cur
        if tok is None:
            raise ConditionError("Unexpected end of expression")
        # Parenthesized expression.
        if tok.kind == "OP" and tok.value == "(":
            self._advance()  # consume "("
            result = self._or_expr()
            self._expect_op(")")
            return result
        return self._atom(tok)

    def _atom(self, tok: _Token) -> bool:
        if tok.kind == "KW":
            self._advance()
            if tok.value == "prev.success":
                return self.prev is not None and self.prev.success
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            raise ConditionError(f"Unknown keyword: {tok.value!r}")  # pragma: no cover
        if tok.kind == "CONTAINS_FILES":
            self._advance()
            needle = tok.value
            if self.prev is None:
                return False
            return any(needle in fc.file_path for fc in self.prev.file_changes)
        if tok.kind == "CONTAINS_SUMMARY":
            self._advance()
            needle = tok.value
            if self.prev is None:
                return False
            return needle in self.prev.summary
        raise ConditionError(f"Unexpected token: {tok}")


# ── Public API ──────────────────────────────────────────────────


def evaluate(condition: str, prev: AgentOutput | None) -> bool:
    """Parse and evaluate a step condition expression.

    Args:
        condition: The expression string. Empty/whitespace = always run.
        prev: The previous step's AgentOutput, or None for step 0.

    Returns:
        True if the step should run, False if it should be skipped.

    Raises:
        ConditionError: If the expression cannot be parsed/evaluated.
    """
    if not condition or not condition.strip():
        return True
    tokens = _tokenize(condition)
    if not tokens:
        return True
    parser = _Parser(tokens, prev)
    return parser.parse()
