# Copyright 2026 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from pants.option.option_types import FileOption, StrOption
from pants.option.subsystem import Subsystem
from pants.util.strutil import softwrap


class TypeScriptProtobufSubsystem(Subsystem):
    options_scope = "typescript-protobuf"
    help = "Options related to the Protobuf TypeScript backend."

    buf_gen_template = FileOption(
        default=None,
        advanced=True,
        help=softwrap(
            """
            Path to the `buf.gen.yaml` template used to generate TypeScript.

            Takes precedence over `[buf].gen_template`. TypeScript needs a template of its
            own because one `buf generate` run executes every plugin in its template, and
            the TypeScript plugins need a Node.js environment that other languages'
            sandboxes do not carry.

            Paths inside the template (`inputs:`, `out:`) are relative to the build root,
            because Pants runs `buf generate` from the sandbox root.
            """
        ),
    )

    buf_node_package_address = StrOption(
        default=None,
        advanced=True,
        help=softwrap(
            """
            Address of the `package_json` target whose `node_modules` provides the
            `protoc-gen-*` plugin binaries named by `buf_gen_template` (for example
            `plugins/typescript:typescript`).

            The package is installed into the sandbox and its `node_modules/.bin` is put on
            `PATH` for the `buf generate` process, so the template can name plugins without
            absolute paths. Its own sources are staged too, so a repo-local plugin can run.
            """
        ),
    )
