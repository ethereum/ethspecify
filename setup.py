import pathlib

from setuptools import setup, find_packages


this_directory = pathlib.Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8")

setup(
    name="ethspecify",
    version="0.3.9",
    description="A utility for processing Ethereum specification tags.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Justin Traglia",
    author_email="jtraglia@pm.me",
    url="https://github.com/ethereum/ethspecify",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "ethspecify=ethspecify.cli:main",
        ],
    },
    install_requires=[
        "requests==2.32.3",
        "PyYAML>=6.0",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
