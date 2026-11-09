"""The XM5 script language: lexer, parser and command catalog binding.

Copied unchanged in substance from GPT's design delivery ``console-20261105/reference/console.py``
(language version 1, catalog revision 3); ``COMMANDS.json`` is that delivery's ``design/COMMANDS.json``.
The window's live dispatcher is ``xm5lib.ui.console_live``; this module never touches the application.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parent / "COMMANDS.json"


class ScriptError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def normalize(name):
    return re.sub(r'[_\-\s]', '', str(name)).casefold()


class Registry:
    def __init__(self, catalog=None):
        self.document = json.loads(Path(catalog or CATALOG).read_text(encoding='utf-8'))
        self.commands = {row['name']: row for row in self.document['commands']}
        self.lookup = {}
        for row in self.commands.values():
            for name in [row['name'], *row.get('aliases', [])]:
                key = normalize(name)
                if key in self.lookup and self.lookup[key] != row['name']:
                    raise RuntimeError('Conflicting command alias: ' + name)
                self.lookup[key] = row['name']

    def resolve(self, name):
        key = normalize(name)
        if key not in self.lookup:
            near = difflib.get_close_matches(key, self.lookup, n=3, cutoff=.55)
            raise ScriptError('UNKNOWN_COMMAND', 'Unknown command: ' + name,
                              {'suggestions': list(dict.fromkeys(self.lookup[x] for x in near))})
        return self.commands[self.lookup[key]]

    def help(self, topic='start'):
        if normalize(topic) in self.lookup:
            return self.resolve(topic)
        if topic in ('start', ''):
            return {'summary': 'Use help("playlist") or commands("tags").',
                    'examples': ['set_volume(35)', 'songs = tracks(artist="Mira")',
                                 'add_to_playlist("Evening", songs)', 'open_album("Glass", artist="Mira")'],
                    'mode': 'existing desktop window; see SUPPORT.json and manuals/START.md',
                    'syntax': 'Calls, named arguments, lists/maps, assignments. Backslashes in strings are literal.'}
        rows = [r for r in self.commands.values()
                if str(topic).casefold().rstrip('s') in (r['group'] + ' ' + r['name'] + ' ' + r['summary']).casefold()]
        return {'topic': topic, 'commands': rows}

    def bind(self, call, evaluate, validate_values=True):
        row = self.resolve(call['name'])
        parameters = row['parameters']
        if len(call['args']) > len(parameters):
            raise ScriptError('ARGUMENTS', 'Too many arguments for ' + row['name'])
        result = {p['name']: copy.deepcopy(p.get('default')) for p in parameters if not p['required']}
        assigned = set()
        for p, value in zip(parameters, call['args']):
            result[p['name']] = evaluate(value)
            assigned.add(p['name'])
        aliases = {'songs': 'items', 'tracks': 'items', 'playlist_name': 'playlist',
                   'percent': 'volume', 'seconds': 'position'}
        canonical = {normalize(p['name']): p['name'] for p in parameters}
        for supplied, value in call['kwargs']:
            name = canonical.get(normalize(supplied))
            if name is None:
                alias = {normalize(k):v for k,v in aliases.items()}.get(normalize(supplied))
                if alias in canonical.values():name=alias
            if name is None:
                raise ScriptError('ARGUMENTS', 'Unknown argument: ' + supplied,
                                  {'command': row['name'], 'accepted': list(canonical.values())})
            if name in assigned:
                raise ScriptError('ARGUMENTS', 'Argument supplied twice: ' + name)
            result[name] = evaluate(value)
            assigned.add(name)
        missing = [p['name'] for p in parameters if p['required'] and p['name'] not in assigned]
        if missing:
            raise ScriptError('ARGUMENTS', 'Missing arguments: ' + ', '.join(missing),
                              {'command': row['name'], 'examples': row['examples']})
        if validate_values:
            for parameter in parameters:
                key=parameter['name']; value=result.get(key); kind=parameter['type']
                if value is None and parameter.get('default','missing') is None:continue
                if kind=='boolean':
                    if type(value) is str and value.casefold() in ('true','yes','on','false','no','off'):
                        value=value.casefold() in ('true','yes','on')
                    elif type(value) is int and value in (0,1):value=bool(value)
                    if type(value) is not bool:raise ScriptError('ARGUMENTS',key+' expects true or false')
                elif kind in ('number','position_or_end'):
                    if kind=='position_or_end' and value=='end':continue
                    if isinstance(value,str) and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):
                        value=float(value) if '.' in value else int(value)
                    if type(value) not in (int,float):raise ScriptError('ARGUMENTS',key+' expects a number')
                elif kind=='list' and not isinstance(value,list):value=[value]
                elif kind=='object' and not isinstance(value,dict):raise ScriptError('ARGUMENTS',key+' expects named fields such as {genre: \"Jazz\"}')
                result[key]=value
        return row['name'], result


def lex(source):
    tokens, i, line = [], 0, 1
    while i < len(source):
        c = source[i]
        if c in ' \t\r':
            i += 1
        elif c=='`' and i+1<len(source) and source[i+1] in ('n','r'):
            if source[i+1]=='n':tokens.append(('SEP','\n',line));line+=1
            i+=2
        elif c == '\n':
            tokens.append(('SEP', '\n', line)); line += 1; i += 1
        elif c == '#':
            while i < len(source) and source[i] != '\n':
                i += 1
        elif c in "\"'":
            quote, start = c, line
            i += 1; value = []
            while i < len(source):
                if source[i] == quote:
                    if i + 1 < len(source) and source[i + 1] == quote:
                        value.append(quote); i += 2; continue
                    i += 1; break
                value.append(source[i]); line += source[i] == '\n'; i += 1
            else:
                raise ScriptError('SYNTAX', 'Unclosed quoted text', {'line': start})
            tokens.append(('VALUE', ''.join(value), start))
        elif c in '(),[]{}=:;':
            # Friendly unquoted positions and Windows paths are single values.
            special = re.match(r'(?:[A-Za-z]:[\\/][^\s,()\[\]{};\"\'#]*|\d+(?::\d+){1,2}(?:\.\d+)?)', source[i:])
            if c == ':' or not special:
                tokens.append(('SEP' if c == ';' else c, c, line)); i += 1
            else:
                value = special.group(); tokens.append(('WORD', value, line)); i += len(value)
        else:
            special = re.match(r'(?:[A-Za-z]:[\\/][^\s,()\[\]{};\"\'#]*|\d+(?::\d+){1,2}(?:\.\d+)?)', source[i:])
            if special:
                value = special.group(); tokens.append(('WORD', value, line)); i += len(value); continue
            end = i
            while end < len(source) and source[end] not in ' \t\r\n(),[]{}=:;#`\"\'':
                end += 1
            if end == i:
                raise ScriptError('SYNTAX', 'Unexpected character', {'line': line, 'character': c})
            value = source[i:end]; i = end
            if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
                number = float(value) if any(x in value.lower() for x in '.e') else int(value)
                tokens.append(('VALUE', number, line))
            else:
                tokens.append(('WORD', value, line))
    tokens.append(('END', '', line))
    return tokens


class Parser:
    def __init__(self, source):
        self.tokens, self.i = lex(source), 0

    def peek(self, kind=None):
        token = self.tokens[self.i]
        return token[0] == kind if kind else token

    def take(self, kind=None):
        token = self.peek()
        if kind and token[0] != kind:
            raise ScriptError('SYNTAX', 'Expected ' + kind, {'line': token[2], 'found': token[1]})
        self.i += 1
        return token

    def skip_lines(self):
        while self.peek('SEP'):
            self.take()

    def arguments(self, closing=None):
        args, kwargs = [], []
        while not self.peek('END') and not (closing and self.peek(closing)):
            if not closing and self.peek('SEP'):
                break
            if closing:
                self.skip_lines()
                if self.peek(closing):
                    break
            if self.peek('WORD') and self.tokens[self.i + 1][0] == '=':
                name = self.take()[1]; self.take('='); kwargs.append((name, self.value()))
            else:
                if kwargs:
                    raise ScriptError('SYNTAX', 'Put positional arguments before named arguments')
                args.append(self.value())
            if self.peek(','):
                self.take(); continue
            if closing and not self.peek(closing) and not self.peek('SEP'):
                raise ScriptError('SYNTAX', 'Separate arguments with a comma', {'line': self.peek()[2]})
        if closing:
            self.take(closing)
        return args, kwargs

    def value(self):
        self.skip_lines()
        kind, value, line = self.take()
        if kind == 'VALUE':
            return ('literal', value)
        if kind == 'WORD':
            if self.peek('('):
                self.take(); args, kwargs = self.arguments(')')
                return ('call', {'name': value, 'args': args, 'kwargs': kwargs, 'line': line})
            return ('word', value)
        if kind == '[':
            result = []
            while True:
                self.skip_lines()
                if self.peek(']'):
                    self.take(); return ('list', result)
                result.append(self.value())
                self.skip_lines()
                if not self.peek(','):
                    self.take(']'); return ('list', result)
                self.take()
        if kind == '{':
            result = []
            while True:
                self.skip_lines()
                if self.peek('}'):
                    self.take(); return ('map', result)
                key = self.take()
                if key[0] not in ('WORD', 'VALUE') or not isinstance(key[1], str):
                    raise ScriptError('SYNTAX', 'A field name must be text', {'line': key[2]})
                self.take(':'); result.append((key[1], self.value()))
                self.skip_lines()
                if not self.peek(','):
                    self.take('}'); return ('map', result)
                self.take()
        raise ScriptError('SYNTAX', 'Expected a value or command', {'line': line, 'found': value})

    def script(self):
        result = []
        while True:
            self.skip_lines()
            if self.peek('END'):
                return result
            assignment = None
            if self.peek('WORD') and self.tokens[self.i + 1][0] == '=':
                assignment = self.take()[1]; self.take('=')
                if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', assignment):
                    raise ScriptError('SYNTAX', 'Use a simple variable name')
            if self.peek('WORD') and self.tokens[self.i + 1][0] not in ('(', 'SEP', 'END'):
                name = self.take(); args, kwargs = self.arguments()
                node = ('call', {'name': name[1], 'args': args, 'kwargs': kwargs, 'line': name[2]})
            else:
                node = self.value()
                if node[0] == 'word':
                    node = ('call', {'name': node[1], 'args': [], 'kwargs': [], 'line': self.peek()[2]})
            if not self.peek('SEP') and not self.peek('END'):
                raise ScriptError('SYNTAX', 'Unexpected trailing input', {'line': self.peek()[2]})
            result.append((assignment, node))


__all__ = ['ScriptError', 'Registry', 'Parser', 'lex', 'normalize', 'CATALOG']
