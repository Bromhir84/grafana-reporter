import logging
import yaml
import re
import os
from datetime import datetime
import pandas as pd
from .prometheus_utils import query_prometheus_range

logger = logging.getLogger(__name__)


class RecordingRuleBackfill:
    def __init__(self, yaml_path="runai_rules.yaml"):
        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Recording rules YAML not found: {yaml_path}")
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        # Flatten all groups into {rule_name: expr}
        self.rules = {}
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if "record" in rule and "expr" in rule:
                    self.rules[rule["record"]] = rule["expr"]

        logger.info(f"Loaded {len(self.rules)} recording rules")

    def _find_dependencies(self, expr: str):
        """
        Return list of recording rules used inside this expression.
        """
        deps = []
        for rule in self.rules.keys():
            if re.search(rf"\b{re.escape(rule)}\b", expr):
                deps.append(rule)
        return deps

    def resolve_rule(self, rule_name: str, start: datetime, end: datetime, step: int = 3600):
        """
        Resolve a recording rule recursively. If dependencies are other recording rules,
        resolve them first. At each level, query Prometheus.
        """
        if rule_name not in self.rules:
            logger.warning(f"No recording rule found for {rule_name}")
            return None

        expr = self.rules[rule_name]
        deps = self._find_dependencies(expr)

        if not deps:
            # Raw-level expression -> query directly
            logger.debug(f"Querying raw expression for {rule_name}: {expr}")
            return query_prometheus_range(expr, start, end, step)

        # Resolve dependencies first
        for dep in deps:
            logger.debug(f"{rule_name} depends on {dep}, resolving...")
            self.resolve_rule(dep, start, end, step)

        # Now query this rule’s expression (with deps intact)
        logger.debug(f"Querying resolved expression for {rule_name}: {expr}")
        return query_prometheus_range(expr, start, end, step)

    def resolve_expression(self, expr: str, start: datetime, end: datetime, step: int = 3600):
        """
        Resolve an arbitrary Grafana/PromQL expression:
        - If it uses recording rules, resolve them recursively.
        - Always end by querying Prometheus for the final expression.
        """
        deps = self._find_dependencies(expr)
        if not deps:
            logger.debug(f"Querying raw Grafana expression: {expr}")
            return query_prometheus_range(expr, start, end, step)

        # Resolve dependencies first
        for dep in deps:
            logger.debug(f"Grafana expression depends on {dep}, resolving...")
            self.resolve_rule(dep, start, end, step)

        # Finally, query the top-level expression
        logger.debug(f"Querying Grafana expression after resolving deps: {expr}")
        return query_prometheus_range(expr, start, end, step)
