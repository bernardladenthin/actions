# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The library behind bernardladenthin/actions. Standard library only, Python 3.9+.

Two subpackages, by what a module knows about:

- ``actionlib.common`` -- nothing about a language or a package format: the workflow job graph,
  the release gate, ``bash -n`` over run scripts, the repository templates, detached signatures,
  the rolling GitHub snapshot.
- ``actionlib.java`` -- Maven and the JVM: the signed Maven publication (Central staging, the
  frozen package, the transfers), the release asset contract, Maven version drift between
  repositories, the license texts of runtime JARs.

A future ecosystem gets a subpackage of its own next to ``java`` and reuses ``common``.

Every module with a command line runs as ``python3 -m actionlib.<package>.<module>``; the composite
actions put ``lib/`` on ``PYTHONPATH`` themselves. Each repository configures the checks in
``.github/actions-config.json`` (``actionlib.common.config``), nothing about a repository is
written into this code.

Run the tests from the repository root:
  python3 -m unittest discover -s lib/tests -t lib
"""
