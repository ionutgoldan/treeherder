"""
TODO: Extract this module into a dedicated PyPI package, acting as the
    Python variant of https://github.com/taskcluster/taskcluster-lib-scopes
"""

import re


def satisfiesExpression(scopeset, expression):
    if not isinstance(scopeset, list):
        raise TypeError("Scopeset must be an array.")

    def isSatisfied(expr):
        if isinstance(expr, str):
            return any([patternMatch(s, expr) for s in scopeset])

        return (
            "AllOf" in expr and all([isSatisfied(e) for e in expr["AllOf"]]) or
            "AnyOf" in expr and any([isSatisfied(e) for e in expr["AnyOf"]])
        )

    return isSatisfied(expression)


def patternMatch(pattern: str, scope):
    if scope == pattern:
        return True

    if re.search(r"\*$", pattern):
        return scope.find(pattern[:-1]) == 0

    return False
