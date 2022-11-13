import os
import platform
import setuptools

try:
    import wheel.bdist_wheel
except ImportError:
    wheel = None


kwargs = {}

with open("delta_net/__init__.py") as f:
    ns = {}
    exec(f.read(), ns)
    version = ns["version"]

with open("README.rst") as f:
    kwargs["long_description"] = f.read()
    kwargs["long_description_content_type"] = "text/x-rst"

if (
    platform.python_implementation() == "CPython"
    and os.environ.get("DELTA_EXTENSION") != "0"
):
    # This extension builds and works on pypy as well, although pypy's jit
    # produces equivalent performance.
    kwargs["ext_modules"] = [
        setuptools.Extension(
            "delta_net.speedups",
            sources=["delta_net/speedups.c"],
            # Unless the user has specified that the extension is mandatory,
            # fall back to the pure-python implementation on any build failure.
            optional=os.environ.get("DELTA_EXTENSION") != "1",
            # Use the stable ABI so our wheels are compatible across python
            # versions.
            py_limited_api=True,
            define_macros=[("Py_LIMITED_API", "0x03070000")],
        )
    ]

if wheel is not None:
    # From https://github.com/joerick/python-abi3-package-sample/blob/main/setup.py
    class bdist_wheel_abi3(wheel.bdist_wheel.bdist_wheel):
        def get_tag(self):
            python, abi, plat = super().get_tag()

            if python.startswith("cp"):
                return "cp37", "abi3", plat
            return python, abi, plat

    kwargs["cmdclass"] = {"bdist_wheel": bdist_wheel_abi3}


setuptools.setup(
    name="delta_net",
    version=version,
    python_requires=">= 3.7",
    packages=["delta_net", "delta_net.test", "delta_net.platform"],
    package_data={
        # data files need to be listed both here (which determines what gets
        # installed) and in MANIFEST.in (which determines what gets included
        # in the sdist tarball)
        "delta_net": ["py.typed"],
        "delta_net.test": [
            "README",
            "csv_translations/fr_FR.csv",
            "gettext_translations/fr_FR/LC_MESSAGES/delta_net_test.mo",
            "gettext_translations/fr_FR/LC_MESSAGES/delta_net_test.po",
            "options_test.cfg",
            "options_test_types.cfg",
            "options_test_types_str.cfg",
            "static/robots.txt",
            "static/sample.xml",
            "static/sample.xml.gz",
            "static/sample.xml.bz2",
            "static/dir/index.html",
            "static_foo.txt",
            "templates/utf8.html",
            "test.crt",
            "test.key",
        ],
    },
    author="EnjoyCloud",
    author_email="enjoycloud@foxmail.com",
    url="http://www.enjoycloud.top/",
    project_urls={
        "Source": "https://github.com/enjoyclouddev/delta_network",
    },
    license="http://www.apache.org/licenses/LICENSE-2.0",
    description=(
        "DeltaNetwork is a Python web framework and asynchronous networking library,"
        " originally developed at FriendFeed."
    ),
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
    ],
    **kwargs
)
