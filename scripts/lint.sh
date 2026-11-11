#!/usr/bin/env bash

set -x

mypy .
black --check .
ruff check .
