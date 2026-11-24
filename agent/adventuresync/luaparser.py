"""Safe parser for WoW SavedVariables files.

Parses only data: `Name = <value>` assignments where a value is a string,
number, boolean, nil or table. Nothing is ever executed.
"""
from typing import Any, Dict


class LuaParseError(ValueError):
    pass


_ESC = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11,
        "\\": 92, '"': 34, "'": 39, "\n": 10}


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    def err(self, msg: str) -> LuaParseError:
        line = self.s.count("\n", 0, self.i) + 1
        return LuaParseError(f"{msg} at line {line}")

    def ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif s.startswith("--", self.i):
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
            else:
                return

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < self.n else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.err(f"expected {ch!r}")
        self.i += 1

    def name(self) -> str:
        self.ws()
        j = self.i
        while j < self.n and (self.s[j].isalnum() or self.s[j] == "_"):
            j += 1
        if j == self.i or self.s[self.i].isdigit():
            raise self.err("expected identifier")
        out = self.s[self.i:j]
        self.i = j
        return out

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        buf = bytearray()
        s = self.s
        while True:
            if self.i >= self.n:
                raise self.err("unterminated string")
            c = s[self.i]
            if c == quote:
                self.i += 1
                break
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    raise self.err("unterminated string")
                c = s[self.i]
                if c.isdigit():
                    j = self.i
                    while j < self.n and j - self.i < 3 and s[j].isdigit():
                        j += 1
                    val = int(s[self.i:j])
                    if val > 255:
                        raise self.err("bad escape")
                    buf.append(val)
                    self.i = j
                elif c in _ESC:
                    buf.append(_ESC[c])
                    self.i += 1
                else:
                    raise self.err("bad escape")
            else:
                buf += c.encode("utf-8")
                self.i += 1
        return buf.decode("utf-8", errors="replace")

    def number(self):
        s, j = self.s, self.i
        if j < self.n and s[j] == "-":
            j += 1
        if s[j:j + 2].lower() == "0x":
            j += 2
            while j < self.n and s[j] in "0123456789abcdefABCDEF":
                j += 1
            tok = s[self.i:j]
            self.i = j
            return int(tok, 16)
        while j < self.n and (s[j].isdigit() or s[j] in ".eE" or
                              (s[j] in "+-" and s[j - 1] in "eE")):
            j += 1
        tok = s[self.i:j]
        self.i = j
        try:
            return int(tok)
        except ValueError:
            try:
                return float(tok)
            except ValueError:
                raise self.err(f"bad number {tok!r}")

    def value(self) -> Any:
        c = self.peek()
        if not c:
            raise self.err("unexpected end of file")
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string()
        if c.isdigit() or c in "-.":
            return self.number()
        if c.isalpha():
            word = self.name()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "nil":
                return None
            if word in ("inf", "nan"):
                return None
            raise self.err(f"unexpected {word!r}")
        raise self.err("unexpected character" if c else "unexpected end of file")

    def table(self) -> Any:
        self.expect("{")
        out: Dict[Any, Any] = {}
        pos = 1
        while True:
            c = self.peek()
            if not c:
                raise self.err("unterminated table")
            if c == "}":
                self.i += 1
                break
            if c == "[":
                self.i += 1
                key = self.value()
                self.expect("]")
                self.expect("=")
                out[key] = self.value()
            else:
                save = self.i
                keyed = False
                if c.isalpha() or c == "_":
                    word = self.name()
                    if self.peek() == "=":
                        self.i += 1
                        out[word] = self.value()
                        keyed = True
                    else:
                        self.i = save
                if not keyed:
                    out[pos] = self.value()
                    pos += 1
            c = self.peek()
            if c and c in ",;":
                self.i += 1
            elif c != "}":
                raise self.err("expected ',' or '}'")
        # Lua arrays (keys exactly 1..n) become lists; an empty table becomes [].
        n = len(out)
        if all(isinstance(k, int) and not isinstance(k, bool) for k in out) and \
                set(out) == set(range(1, n + 1)):
            return [out[k] for k in range(1, n + 1)]
        return out


def loads(text: str) -> Dict[str, Any]:
    """Return {global_name: value} for every top-level assignment."""
    p = _Parser(text)
    result: Dict[str, Any] = {}
    while p.peek():
        name = p.name()
        p.expect("=")
        result[name] = p.value()
        if p.peek() == ";":
            p.i += 1
    return result
