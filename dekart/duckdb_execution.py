import re
import sys
from pathlib import Path


SUPPORTED_SOURCE_EXTENSIONS = {"csv", "parquet", "json", "geojson"}
DOUBLE_TYPES = {"BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT"}


def validate_prepared_execution(result):
    """Validate a server-issued DuckDB program before local side effects."""
    if not isinstance(result, dict):
        raise ValueError("run_query returned an invalid result.")
    query_job = result.get("query_job")
    execution = result.get("duckdb_execution")
    if not isinstance(query_job, dict) or not isinstance(execution, dict):
        raise ValueError("DuckDB execution requires query_job and duckdb_execution.")

    for field in ("id", "query_id", "dataset_id", "query_params_hash"):
        if not str(query_job.get(field, "")).strip():
            raise ValueError("DuckDB query_job.{0} is missing.".format(field))
    if str(query_job.get("job_status", "")).strip().upper() != "JOB_STATUS_DONE":
        raise ValueError("DuckDB query_job.job_status must be JOB_STATUS_DONE.")
    if str(query_job.get("job_error", "")).strip():
        raise ValueError("DuckDB query_job.job_error must be empty.")

    duckdb_version = str(execution.get("duckdb_version", "")).strip()
    if not duckdb_version:
        raise ValueError("duckdb_execution.duckdb_version is missing.")
    sources = execution.get("sources")
    statements = execution.get("statements")
    if not isinstance(sources, list):
        raise ValueError("duckdb_execution.sources must be an array.")
    if not isinstance(statements, list) or not statements:
        raise ValueError("duckdb_execution.statements must be a non-empty array.")

    dataset_ids = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError("duckdb_execution.sources[{0}] is invalid.".format(index))
        dataset_id = str(source.get("dataset_id", "")).strip()
        if not dataset_id or dataset_id in dataset_ids:
            raise ValueError("duckdb_execution.sources[{0}].dataset_id is missing or duplicated.".format(index))
        dataset_ids.add(dataset_id)
        revisions = [
            str(source.get(field, "")).strip()
            for field in ("file_source_id", "query_job_id")
            if str(source.get(field, "")).strip()
        ]
        if len(revisions) != 1:
            raise ValueError("duckdb_execution.sources[{0}] must contain exactly one revision.".format(index))
        extension = str(source.get("extension", "")).strip().lower()
        if extension not in SUPPORTED_SOURCE_EXTENSIONS:
            raise ValueError("duckdb_execution.sources[{0}].extension is unsupported; update the Dekart CLI.".format(index))

    for index, statement in enumerate(statements):
        if not isinstance(statement, dict) or not str(statement.get("sql", "")).strip():
            raise ValueError("duckdb_execution.statements[{0}].sql is missing.".format(index))
        parameters = statement.get("parameters", [])
        if not isinstance(parameters, list) or any(not isinstance(value, str) for value in parameters):
            raise ValueError("duckdb_execution.statements[{0}].parameters is invalid.".format(index))
    return query_job, execution


def warn_on_version_mismatch(expected_version, actual_version):
    """Warn without blocking when browser and native DuckDB versions differ."""
    if str(expected_version).strip() == str(actual_version).strip():
        return
    print(
        "Warning: Dekart prepared this query for DuckDB {0}, but this CLI uses {1}. "
        "Continuing best effort. Update with `python -m pip install --upgrade dekart` "
        "and use the latest Dekart version.".format(expected_version, actual_version),
        file=sys.stderr,
    )


def quote_identifier(value):
    """Quote one DuckDB identifier."""
    return '"{0}"'.format(str(value).replace('"', '""'))


def root_table_reference(job_id):
    """Derive the canonical internal table for the validated root job."""
    normalized = str(job_id).replace("-", "_")
    if not re.fullmatch(r"[A-Za-z0-9_]+", normalized):
        raise ValueError("DuckDB query_job.id is invalid.")
    return "dekart_internal.{0}".format(quote_identifier("job_" + normalized))


def publication_query(connection, table_reference):
    """Build the browser-equivalent result projection from DuckDB types."""
    columns = connection.execute("DESCRIBE SELECT * FROM {0}".format(table_reference)).fetchall()
    projection = []
    for row in columns:
        name = str(row[0])
        type_name = str(row[1]).upper()
        quoted = quote_identifier(name)
        if type_name == "GEOMETRY":
            projection.append("ST_AsWKB({0}) AS {0}".format(quoted))
        elif type_name in DOUBLE_TYPES or type_name.startswith("DECIMAL"):
            projection.append("CAST({0} AS DOUBLE) AS {0}".format(quoted))
        else:
            projection.append(quoted)
    return "SELECT {0} FROM {1}".format(", ".join(projection), table_reference)


def execute_program(execution, query_job, source_paths, result_path, work_dir):
    """Execute opaque prepared statements once and publish the root as Parquet."""
    if sys.version_info < (3, 9):
        raise RuntimeError("DuckDB query execution requires Python 3.9 or newer.")
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        raise RuntimeError("DuckDB query execution requires the duckdb Python package.") from exc

    warn_on_version_mismatch(execution["duckdb_version"], duckdb.__version__)
    work_path = Path(work_dir).resolve()
    result_path = Path(result_path).resolve()
    connection = duckdb.connect(
        str(work_path / "execution.duckdb"),
        config={"allow_unsigned_extensions": "false"},
    )
    try:
        connection.execute("SET autoinstall_known_extensions=false")
        connection.execute("SET autoload_known_extensions=false")
        connection.execute("SET allow_community_extensions=false")
        connection.execute("INSTALL spatial")
        connection.execute("LOAD spatial")
        connection.execute("INSTALL parquet")
        connection.execute("LOAD parquet")
        connection.execute("SET allowed_directories = ?", [[str(work_path)]])
        connection.execute("SET temp_directory = ?", [str(work_path / "tmp")])
        connection.execute("SET enable_external_access=false")
        for index, path in enumerate(source_paths):
            connection.execute(
                "SET VARIABLE dekart_source_{0}_path = ?".format(index),
                [str(Path(path).resolve())],
            )
        connection.execute("SET VARIABLE dekart_result_path = ?", [str(result_path)])
        connection.execute("SET lock_configuration=true")

        for index, statement in enumerate(execution["statements"]):
            try:
                connection.execute(statement["sql"], statement.get("parameters", []))
            except Exception as exc:
                raise RuntimeError("DuckDB statement {0} failed: {1}".format(index + 1, exc)) from exc

        table_reference = root_table_reference(query_job["id"])
        projection = publication_query(connection, table_reference)
        connection.execute(
            "COPY ({0}) TO (getvariable('dekart_result_path')) (FORMAT PARQUET)".format(projection)
        )
        connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(result_path)])
    finally:
        connection.close()
