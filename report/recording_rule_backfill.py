import logging
import yaml
import re
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from .prometheus_utils import query_prometheus_range

logger = logging.getLogger(__name__)


class RecordingRuleBackfill:
    def __init__(self, yaml_path=None):
        if yaml_path is None:
            yaml_path = os.path.join(os.path.dirname(__file__), "runai.yaml")
        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Recording rules YAML not found: {yaml_path}")

        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        # Flatten all groups into {rule_name: expr}
        self.rules_map = {}
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if "record" in rule and "expr" in rule:
                    self.rules_map[rule["record"]] = rule["expr"]

        logger.info(f"Loaded {len(self.rules_map)} recording rules")

    def _find_dependencies(self, expr: str):
        """
        Return list of recording rules used inside this expression.
        """
        deps = []
        for rule in self.rules_map.keys():
            if re.search(rf"\b{re.escape(rule)}\b", expr):
                deps.append(rule)
        return deps

    def recompute_rule_for_timeframe(self, rule_name: str, start: datetime, end: datetime, step: int = 3600):
        """
        Recompute a recording rule for a given timeframe (start, end).
        Returns a Pandas DataFrame with columns: ['project', 'department', <rule_name>]
        """
        if rule_name not in self.rules_map:
            logger.warning(f"No recording rule found for {rule_name}")
            return pd.DataFrame(columns=["project", "department", rule_name])

        expr = self.rules_map[rule_name]
        deps = self._find_dependencies(expr)

        if not deps:
            # Raw-level expression -> query Prometheus directly
            logger.debug(f"Querying raw metric for {rule_name}: {expr}")
            resp = query_prometheus_range(expr, start, end, step)
            return self._prometheus_result_to_df(resp, rule_name)

        # Resolve dependencies recursively
        dep_dfs = {}
        for dep in deps:
            dep_dfs[dep] = self.recompute_rule_for_timeframe(dep, start, end, step)

        # Replace dependent rules in expr with temporary Pandas DataFrames
        # For simplicity, only handle simple `sum by(...) / scalar` patterns
        # This can be extended for more complex PromQL
        df = dep_dfs[deps[0]].copy()
        df[rule_name] = df[deps[0]]  # Copy dependent column
        df.drop(columns=deps[0], inplace=True)

        # If expression contains division by scalar
        match = re.search(r"/\s*([\d\.]+)", expr)
        if match:
            divisor = float(match.group(1))
            df[rule_name] = df[rule_name] / divisor

        # Note: Only sum by (project, department) is currently supported
        return df

    def _prometheus_result_to_df(self, results, column_name):
        """
        Convert Prometheus query result JSON to DataFrame
        """
        rows = []
        for r in results.get("data", {}).get("result", []):
            metric_labels = r.get("metric", {})
            project = metric_labels.get("project", "unknown")
            department = metric_labels.get("department", "unknown")
            if r.get("values"):
                # Take last value in range
                _, value = r["values"][-1]
                rows.append({"project": project, "department": department, column_name: float(value)})
        if not rows:
            # Return zero values if nothing exists
            rows.append({"project": "unknown", "department": "unknown", column_name: 0.0})
        return pd.DataFrame(rows)
