"""The stacks `factory init` knows, as a table of data, so a new stack is one entry of `STACKS`.

The commands are the shipped Pipeline's `vars` for a project of the stack with its usual test tools.
Each report goes where the Gates read it, into `$GATE_OUT`, and a command that copies reports keeps the exit code of its
tests, so a red test still fails the Gate after the reports are there.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Stack:
    """A Stack is what `factory init` fills in when the repository has every file of `marks`, each a glob pattern."""

    name: str
    marks: tuple[str, ...]
    build: str
    test: str
    junit: str
    coverage_format: str
    coverage_report: str
    egress: tuple[str, ...] = ()
    caches: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


STACKS = (
    Stack(
        name="Python with uv",
        marks=("pyproject.toml", "uv.lock"),
        build="uv sync --locked",
        test='uv run --locked pytest --junitxml="$GATE_OUT/junit.xml" --cov --cov-report=lcov:"$GATE_OUT/lcov.info"',
        junit="junit.xml",
        coverage_format="lcov",
        coverage_report="lcov.info",
        egress=("pypi.org", "files.pythonhosted.org"),
        caches=(".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
        # `uv run` would otherwise build a Linux `.venv` in the Workspace, which breaks `uv` on the host.
        env={"UV_PROJECT_ENVIRONMENT": "/home/agent/venv"},
    ),
    Stack(
        name="Node",
        marks=("package.json",),
        build="npm ci",
        test=(
            'JEST_JUNIT_OUTPUT_FILE="$GATE_OUT/junit.xml" npx jest --ci --reporters=jest-junit'
            ' --coverage --coverageReporters=lcov --coverageDirectory="$GATE_OUT"'
        ),
        junit="junit.xml",
        coverage_format="lcov",
        coverage_report="lcov.info",
        egress=("registry.npmjs.org",),
        caches=("node_modules", "coverage", ".npm"),
    ),
    Stack(
        name="Go",
        marks=("go.mod",),
        build="go build ./...",
        test=(
            'go run gotest.tools/gotestsum@v1.12.3 --junitfile "$GATE_OUT/junit.xml"'
            ' -- -coverprofile="$GATE_OUT/cover.out" ./...'
        ),
        junit="junit.xml",
        coverage_format="gocover",
        coverage_report="cover.out",
        egress=("proxy.golang.org", "sum.golang.org"),
    ),
    Stack(
        name="Rust with Cargo",
        marks=("Cargo.toml",),
        build="cargo build --locked",
        test=(
            'cargo llvm-cov nextest --profile ci --lcov --output-path "$GATE_OUT/lcov.info"; code=$?;'
            ' cp target/nextest/ci/junit.xml "$GATE_OUT/"; exit $code'
        ),
        junit="junit.xml",
        coverage_format="lcov",
        coverage_report="lcov.info",
        egress=("crates.io", "index.crates.io", "static.crates.io"),
        caches=("target",),
    ),
    Stack(
        name="Gradle",
        marks=("build.gradle*",),
        build="./gradlew assemble",
        test=(
            "./gradlew test jacocoTestReport; code=$?;"
            ' cp build/test-results/test/*.xml "$GATE_OUT/";'
            ' cp build/reports/jacoco/test/jacocoTestReport.xml "$GATE_OUT/"; exit $code'
        ),
        junit="TEST-*.xml",
        coverage_format="jacoco",
        coverage_report="jacocoTestReport.xml",
        egress=("services.gradle.org", "plugins.gradle.org", "repo.maven.apache.org"),
        caches=("build", ".gradle"),
    ),
    Stack(
        name="Maven",
        marks=("pom.xml",),
        build="mvn -B -DskipTests package",
        test=(
            "mvn -B verify; code=$?;"
            ' cp target/surefire-reports/*.xml "$GATE_OUT/"; cp target/site/jacoco/jacoco.xml "$GATE_OUT/"; exit $code'
        ),
        junit="TEST-*.xml",
        coverage_format="jacoco",
        coverage_report="jacoco.xml",
        egress=("repo.maven.apache.org",),
        caches=("target",),
    ),
    Stack(
        name="CMake",
        marks=("CMakeLists.txt",),
        build="cmake -B build -DCMAKE_CXX_FLAGS=--coverage && cmake --build build",
        test=(
            'ctest --test-dir build --output-junit "$GATE_OUT/junit.xml"; code=$?;'
            ' gcovr --cobertura "$GATE_OUT/cobertura.xml"; exit $code'
        ),
        junit="junit.xml",
        coverage_format="cobertura",
        coverage_report="cobertura.xml",
        caches=("build",),
    ),
)


def detected(repo: Path) -> Stack | None:
    """Returns the first Stack whose files the repository has, or None."""
    return next((stack for stack in STACKS if all(any(repo.glob(mark)) for mark in stack.marks)), None)
