# Copyright 2026 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Generate TypeScript from Protobuf via `buf generate`."""

from __future__ import annotations

from collections.abc import Iterable

from pants.backend.codegen.protobuf import target_types as protobuf_target_types
from pants.backend.codegen.protobuf.target_types import ProtobufSourcesGeneratorTarget
from pants.backend.codegen.protobuf.typescript import buf_rules
from pants.backend.javascript import install_node_package
from pants.engine.rules import Rule
from pants.engine.target import Target
from pants.engine.unions import UnionRule


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *buf_rules.rules(),
        *install_node_package.rules(),
        *protobuf_target_types.rules(),
    )


def target_types() -> Iterable[type[Target]]:
    return (ProtobufSourcesGeneratorTarget,)
