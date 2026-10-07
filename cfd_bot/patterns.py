"""Reusable log-pattern presets shared by the ticket editor."""
from . import diagnostics as _diagnostics
from copy import deepcopy
from pathlib import Path

from .config import boolean, keys, patterns, read_json
from .tickets import atomic_json, ticket_lock
from .ui import load_ui

DEFAULT_NAME = load_ui().text('scenarios.diagnostics.patterns.default_name')
DEFAULT_RULES = {
    'failure_patterns': [], 'openfoam_defaults': True,
}


@_diagnostics.trace
def validate_rules(rules):
    # Read legacy template fields so existing files remain loadable, then drop
    # them because successful completion is now determined from controlDict.
    ui = load_ui()
    keys(rules, 'success_patterns success_match failure_patterns openfoam_defaults',
         ui.text('scenarios.diagnostics.patterns.template'))
    patterns(rules.get('failure_patterns'), ui.text('scenarios.diagnostics.patterns.failure'))
    boolean(rules.get('openfoam_defaults'), ui.text('scenarios.diagnostics.patterns.openfoam'))
    if 'success_patterns' in rules:
        if _diagnostics.enabled: _diagnostics.step('patterns.validate_rules:L23:then')
        patterns(rules['success_patterns'], ui.text('scenarios.diagnostics.patterns.legacy_success'))
    if 'success_match' in rules and rules['success_match'] not in ('all', 'any'):
        if _diagnostics.enabled: _diagnostics.step('patterns.validate_rules:L25:then')
        raise ValueError(ui.text('scenarios.diagnostics.patterns.success_match'))
    return {key: deepcopy(rules[key]) for key in DEFAULT_RULES}


class PatternLibrary:
    @_diagnostics.trace
    def __init__(self, path):
        self.path = Path(path)

    @_diagnostics.trace
    def load(self):
        result = {DEFAULT_NAME: deepcopy(DEFAULT_RULES)}
        if not self.path.exists():
            if _diagnostics.enabled: _diagnostics.step('patterns.PatternLibrary.load:L36:then')
            return result
        document = read_json(self.path)
        ui = load_ui()
        keys(document, 'version templates', ui.text('scenarios.diagnostics.patterns.file'))
        if type(document.get('version')) is not int or document['version'] != 1 or not isinstance(document.get('templates'), dict):
            if _diagnostics.enabled: _diagnostics.step('patterns.PatternLibrary.load:L41:then')
            raise ValueError(ui.text('scenarios.diagnostics.patterns.file_format'))
        for name, rules in document['templates'].items():
            if _diagnostics.enabled: _diagnostics.step('patterns.PatternLibrary.load:L43:loop', name=name, rules=rules)
            if not name.strip() or len(name) > 80 or name == DEFAULT_NAME:
                if _diagnostics.enabled: _diagnostics.step('patterns.PatternLibrary.load:L44:then')
                raise ValueError(ui.text('scenarios.diagnostics.patterns.name_invalid'))
            result[name] = validate_rules(rules)
        return result

    @_diagnostics.trace
    def save(self, name, rules):
        name = name.strip()
        if not name or len(name) > 80 or name == DEFAULT_NAME:
            if _diagnostics.enabled: _diagnostics.step('patterns.PatternLibrary.save:L51:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.patterns.name_required'))
        rules = validate_rules(rules)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with ticket_lock(self.path.parent):
            templates = self.load()
            templates.pop(DEFAULT_NAME)
            templates[name] = rules
            atomic_json(self.path, {'version': 1, 'templates': templates})
