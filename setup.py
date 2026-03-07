# setup.py is kept for Frappe v14/v15 compatibility.
# Frappe v16+ uses pyproject.toml — see pyproject.toml in this directory.
from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="connector",
    version="1.0.0",
    description="ERPNext integration connector — Magento, multi-site ERPNext sync, and more",
    author="Bookspot",
    author_email="info@bookspot.co.ke",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    python_requires=">=3.11",
    install_requires=install_requires,
)
