import yaml
import logging
from .prometheus_utils import query_prometheus_range
from datetime import datetime

logger = logging.getLogger(__name__)

class RecordingRuleBackfill:
    def __init__(self, yaml_path):
        self.yaml_path = yaml_path
        self.rules_map = {}  # record_name -> expr
        self._load_yaml()

    def _load_yaml(self):
        """Load recording rules YAML and build a mapping."""
        with open(self.yaml_path) as f:
            rules_yaml = yaml.safe_load(f)

        for group in rules_yaml.get("groups", []):
            for rule in group.get("rules", []):
                record_name = rule.get("record")
                expr = rule.get("expr")
                if record_name and expr:
                    self.rules_map[record_name] = expr

        logger.info(f"Loaded {len(self.rules_map)} recording rules from YAML")

    def backfill_rule(self, record_name, start: datetime, end: datetime, step: int = 3600, seen=None):
        """
        Recursively backfill a recording rule by querying Prometheus.
        Returns results in Prometheus API format (dict).
        """
        if seen is None:
            seen = set()

        if record_name in seen:
            logger.warning(f"Detected circular dependency on rule: {record_name}")
            return {"data": {"result": []}}  # prevent infinite recursion

        seen.add(record_name)

        expr = self.rules_map.get(record_name)
        if not expr:
            logger.warning(f"No expression found for recording rule: {record_name}")
            return {"data": {"result": []}}

        # Check if expr references another recording rule
        # e.g., look for any word in rules_map keys in expr
        for dep_name in self.rules_map:
            if dep_name in expr:
                logger.info(f"Rule {record_name} depends on {dep_name}, resolving recursively")
                # Backfill the dependency
                dep_result = self.backfill_rule(dep_name, start, end, step, seen)
                # You can implement logic here to merge dep_result into expr evaluation if needed
                # For now, we just ensure dep_rule is resolved before querying

        logger.info(f"Querying Prometheus for recording rule: {record_name} -> {expr}")
        try:
            results = query_prometheus_range(expr, start=start, end=end, step=step)
        except Exception as e:
            logger.error(f"Failed to query Prometheus for {record_name}: {e}")
            results = {"data": {"result": []}}

        # If results are empty, you can optionally fill zeros
        if not results.get("data", {}).get("result"):
            logger.warning(f"No data returned for {record_name}")
            # Could return zeros here if you want to auto-fill downstream
            # results = self._fill_zeros()  # implement if needed

        return results
