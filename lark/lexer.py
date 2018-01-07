## Lexer Implementation

import re

from .utils import Str, classify
from .common import is_terminal, PatternStr, PatternRE, TokenDef

class LexError(Exception):
    pass

class UnexpectedInput(LexError):
    def __init__(self, seq, lex_pos, line, column, allowed=None):
        context = seq[lex_pos:lex_pos+5]
        message = "No token defined for: '%s' in %r at line %d col %d" % (seq[lex_pos], context, line, column)

        super(UnexpectedInput, self).__init__(message)

        self.line = line
        self.column = column
        self.context = context
        self.allowed = allowed

class Token(Str):
    def __new__(cls, type_, value, pos_in_stream=None, line=None, column=None):
        inst = Str.__new__(cls, value)
        inst.type = type_
        inst.pos_in_stream = pos_in_stream
        inst.value = value
        inst.line = line
        inst.column = column
        return inst

    @classmethod
    def new_borrow_pos(cls, type_, value, borrow_t):
        return cls(type_, value, borrow_t.pos_in_stream, line=borrow_t.line, column=borrow_t.column)

    def __repr__(self):
        return 'Token(%s, %r)' % (self.type, self.value)

    def __deepcopy__(self, memo):
        return Token(self.type, self.value, self.pos_in_stream, self.line, self.column)

    def __eq__(self, other):
        if isinstance(other, Token) and self.type != other.type:
            return False

        return Str.__eq__(self, other)

    __hash__ = Str.__hash__

class Regex:
    def __init__(self, pattern, flags=()):
        self.pattern = pattern
        self.flags = flags

def _regexp_has_newline(r):
    return '\n' in r or '\\n' in r or ('(?s)' in r and '.' in r)

def _create_unless_callback(strs):
    mres = build_mres(strs, match_whole=True)
    def unless_callback(t):
        # if t in strs:
        #     t.type = strs[t]
        for mre, type_from_index in mres:
            m = mre.match(t.value)
            if m:
                value = m.group(0)
                t.type = type_from_index[m.lastindex]
                break
        return t
    return unless_callback

def _create_unless(tokens):
    tokens_by_type = classify(tokens, lambda t: type(t.pattern))
    assert len(tokens_by_type) <= 2, tokens_by_type.keys()
    embedded_strs = set()
    callback = {}
    for retok in tokens_by_type.get(PatternRE, []):
        unless = [] # {}
        for strtok in tokens_by_type.get(PatternStr, []):
            s = strtok.pattern.value
            m = re.match(retok.pattern.to_regexp(), s)
            if m and m.group(0) == s:
                unless.append(strtok)
                if strtok.pattern.flags <= retok.pattern.flags:
                    embedded_strs.add(strtok)
        if unless:
            callback[retok.name] = _create_unless_callback(unless)

    tokens = [t for t in tokens if t not in embedded_strs]
    return tokens, callback


def _build_mres(tokens, max_size, match_whole):
    # Python sets an unreasonable group limit (currently 100) in its re module
    # Worse, the only way to know we reached it is by catching an AssertionError!
    # This function recursively tries less and less groups until it's successful.
    postfix = '$' if match_whole else ''
    mres = []
    while tokens:
        try:
            mre = re.compile(u'|'.join(u'(?P<%s>%s)'%(t.name, t.pattern.to_regexp()+postfix) for t in tokens[:max_size]))
        except AssertionError:  # Yes, this is what Python provides us.. :/
            return _build_mres(tokens, max_size//2, match_whole)

        mres.append((mre, {i:n for n,i in mre.groupindex.items()} ))
        tokens = tokens[max_size:]
    return mres

def build_mres(tokens, match_whole=False):
    return _build_mres(tokens, len(tokens), match_whole)


class LineCounter:
    def __init__(self):
        self.newline_char = '\n'
        self.char_pos = 0
        self.line = 1
        self.column = 0
        self.line_start_pos = 0

    def feed(self, token, test_newline=True):
        """Consume a token and calculat the new line & column.

        As an optional optimization, set test_newline=False is token doesn't contain a newline.
        """
        if test_newline:
            newlines = token.count(self.newline_char)
            if newlines:
                self.line += newlines
                self.line_start_pos = self.char_pos + token.rindex(self.newline_char) + 1

        self.char_pos += len(token)
        self.column = self.char_pos - self.line_start_pos



class Lexer:
    def __init__(self, tokens, ignore=()):
        assert all(isinstance(t, TokenDef) for t in tokens), tokens

        self.ignore = ignore
        tokens = list(tokens)

        # Sanitization
        for t in tokens:
            try:
                re.compile(t.pattern.to_regexp())
            except:
                raise LexError("Cannot compile token %s: %s" % (t.name, t.pattern))

            if t.pattern.min_width == 0:
                raise LexError("Lexer does not allow zero-width tokens. (%s: %s)" % (t.name, t.pattern))

        assert set(ignore) <= {t.name for t in tokens}

        # Init
        self.newline_types = [t.name for t in tokens if _regexp_has_newline(t.pattern.to_regexp())]
        self.ignore_types = [t for t in ignore]

        tokens.sort(key=lambda x:(-x.priority, -x.pattern.max_width, -len(x.pattern.value), x.name))

        tokens, self.callback = _create_unless(tokens)
        assert all(self.callback.values())

        self.tokens = tokens

        self.mres = build_mres(tokens)

    def lex(self, stream):
        return _Lex(self).lex(stream, self.newline_types, self.ignore_types)


class ContextualLexer:
    def __init__(self, tokens, states, ignore=(), always_accept=()):
        tokens_by_name = {}
        for t in tokens:
            assert t.name not in tokens_by_name, t
            tokens_by_name[t.name] = t

        lexer_by_tokens = {}
        self.lexers = {}
        for state, accepts in states.items():
            key = frozenset(accepts)
            try:
                lexer = lexer_by_tokens[key]
            except KeyError:
                accepts = set(accepts) | set(ignore) | set(always_accept)
                state_tokens = [tokens_by_name[n] for n in accepts if is_terminal(n) and n!='$END']
                lexer = Lexer(state_tokens, ignore=ignore)
                lexer_by_tokens[key] = lexer

            self.lexers[state] = lexer

        self.root_lexer = Lexer(tokens, ignore=ignore)

        self.set_parser_state(None) # Needs to be set on the outside

    def set_parser_state(self, state):
        self.parser_state = state

    def lex(self, stream):
        l = _Lex(self.lexers[self.parser_state])
        for x in l.lex(stream, self.root_lexer.newline_types, self.root_lexer.ignore_types):
            yield x
            l.lexer = self.lexers[self.parser_state]


class _Lex:
    "Built to serve both Lexer and ContextualLexer"
    def __init__(self, lexer):
        self.lexer = lexer

    def lex(self, stream, newline_types, ignore_types):
        newline_types = list(newline_types)
        newline_types = list(newline_types)
        line_ctr = LineCounter()

        while True:
            lexer = self.lexer
            for mre, type_from_index in lexer.mres:
                m = mre.match(stream, line_ctr.char_pos)
                if m:
                    value = m.group(0)
                    type_ = type_from_index[m.lastindex]
                    if type_ not in ignore_types:
                        t = Token(type_, value, line_ctr.char_pos, line_ctr.line, line_ctr.column)
                        if t.type in lexer.callback:
                            t = lexer.callback[t.type](t)
                        lexer = yield t

                    line_ctr.feed(value, type_ in newline_types)
                    break
            else:
                if line_ctr.char_pos < len(stream):
                    raise UnexpectedInput(stream, line_ctr.char_pos, line_ctr.line, line_ctr.column)
                break

