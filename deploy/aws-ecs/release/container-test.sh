set -eu
python -m pip install --no-deps --target /tmp/testdeps pytest==9.1.1 httpx==0.28.1 httpcore==1.0.9 pluggy==1.6.0 iniconfig==2.3.0 packaging==26.3 pygments==2.21.0
TRACK_NODE_BINARY=$(python -c 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent / "driver" / "node")')
ln -s "$TRACK_NODE_BINARY" /tmp/node
export PATH="/tmp:$PATH"
export PYTHONPATH=/tmp/testdeps:/app/src
export PYTHON_DOTENV_DISABLED=1
python -m pytest /validation/tests -q -o cache_dir=/tmp/pytest-cache
