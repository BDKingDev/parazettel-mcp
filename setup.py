from setuptools import find_packages, setup
setup(
    name="parazettel-mcp",
    version="0.4.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.11",
    include_package_data=True,
    install_requires=[
        "mcp[cli]>=2.3.0,<3",
        "sqlalchemy>=2.0.0",
        "pydantic>=2.0.0",
        "python-frontmatter>=1.0.0",
        "markdown>=3.4.0",
        "python-dotenv>=1.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "isort>=5.12.0",
            "mypy>=1.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "parazettel-mcp=parazettel_mcp.main:main",
        ],
    },
)
