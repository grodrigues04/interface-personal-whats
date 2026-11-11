#!/bin/sh -e
set -x

black .
ruff check . --fix --select I
