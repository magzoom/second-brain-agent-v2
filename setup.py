from setuptools import setup, find_packages

setup(
    name="sba",
    version="2.0.0",
    packages=find_packages(),
    install_requires=open("requirements.txt").read().splitlines(),
    entry_points={"console_scripts": ["sba=sba.cli:cli"]},
)
