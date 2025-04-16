import psutil
import pandas
from pandas import DataFrame
from snowflake.sqlalchemy import snowdialect
from snowflake import connector
from snowflake.connector.errors import NotSupportedError

from mindsdb.utilities import log
from mindsdb_sql_parser.ast.base import ASTNode
from mindsdb_sql_parser.ast import Select, Identifier

from mindsdb.integrations.libs.base import DatabaseHandler
from mindsdb.utilities.render.sqlalchemy_render import SqlalchemyRender
from mindsdb.integrations.libs.response import (
    HandlerStatusResponse as StatusResponse,
    HandlerResponse as Response,
    RESPONSE_TYPE
)
from mindsdb.api.mysql.mysql_proxy.libs.constants.mysql import MYSQL_DATA_TYPE

try:
    import pyarrow as pa
    memory_pool = pa.default_memory_pool()
except Exception:
    memory_pool = None


logger = log.getLogger(__name__)


def _map_type(internal_type_name: str) -> MYSQL_DATA_TYPE:
    """ Map Snowflake types to MySQL types.

    Args:
        internal_type_name (str): The name of the Snowflake type to map.

    Returns:
        MYSQL_DATA_TYPE: The MySQL type that corresponds to the Snowflake type.
    """
    internal_type_name = internal_type_name.upper()
    types_map = {
        ('NUMBER', 'DECIMAL', 'DEC', 'NUMERIC'): MYSQL_DATA_TYPE.DECIMAL,
        ('INT , INTEGER , BIGINT , SMALLINT , TINYINT , BYTEINT'): MYSQL_DATA_TYPE.INT,
        ('FLOAT', 'FLOAT4', 'FLOAT8'): MYSQL_DATA_TYPE.FLOAT,
        ('DOUBLE', 'DOUBLE PRECISION', 'REAL'): MYSQL_DATA_TYPE.DOUBLE,
        ('VARCHAR'): MYSQL_DATA_TYPE.VARCHAR,
        ('CHAR', 'CHARACTER', 'NCHAR'): MYSQL_DATA_TYPE.CHAR,
        ('STRING', 'TEXT', 'NVARCHAR'): MYSQL_DATA_TYPE.TEXT,
        ('NVARCHAR2', 'CHAR VARYING', 'NCHAR VARYING'): MYSQL_DATA_TYPE.VARCHAR,
        ('BINARY', 'VARBINARY'): MYSQL_DATA_TYPE.BINARY,
        ('BOOLEAN',): MYSQL_DATA_TYPE.BOOL,
        ('TIMESTAMP_NTZ', 'DATETIME'): MYSQL_DATA_TYPE.DATETIME,
        ('DATE',): MYSQL_DATA_TYPE.DATE,
        ('TIME',): MYSQL_DATA_TYPE.TIME,
        ('TIMESTAMP_LTZ'): MYSQL_DATA_TYPE.DATETIME,
        ('TIMESTAMP_TZ'): MYSQL_DATA_TYPE.DATETIME,
        ('VARIANT', 'OBJECT', 'ARRAY', 'MAP', 'GEOGRAPHY', 'GEOMETRY', 'VECTOR'): MYSQL_DATA_TYPE.VARCHAR
    }

    for db_types_list, mysql_data_type in types_map.items():
        if internal_type_name in db_types_list:
            return mysql_data_type

    logger.warning(f"Snowflake handler type mapping: unknown type: {internal_type_name}, use VARCHAR as fallback.")
    return MYSQL_DATA_TYPE.VARCHAR


class SnowflakeHandler(DatabaseHandler):
    """
    This handler handles connection and execution of the Snowflake statements.
    """

    name = 'snowflake'

    def __init__(self, name, **kwargs):
        super().__init__(name)
        self.connection_data = kwargs.get('connection_data')
        self.renderer = SqlalchemyRender(snowdialect.dialect)

        self.is_connected = False
        self.connection = None

    def connect(self):
        """
        Establishes a connection to a Snowflake account.

        Raises:
            ValueError: If the required connection parameters are not provided.
            snowflake.connector.errors.Error: If an error occurs while connecting to the Snowflake account.

        Returns:
            snowflake.connector.connection.SnowflakeConnection: A connection object to the Snowflake account.
        """

        if self.is_connected is True:
            return self.connection

        # Mandatory connection parameters
        if not all(key in self.connection_data for key in ['account', 'user', 'password', 'database']):
            raise ValueError('Required parameters (account, user, password, database) must be provided.')

        config = {
            'account': self.connection_data.get('account'),
            'user': self.connection_data.get('user'),
            'password': self.connection_data.get('password'),
            'database': self.connection_data.get('database')
        }

        # Optional connection parameters
        optional_params = ['schema', 'warehouse', 'role']
        for param in optional_params:
            if param in self.connection_data:
                config[param] = self.connection_data[param]

        try:
            self.connection = connector.connect(**config)
            self.connection.telemetry_enabled = False
            self.is_connected = True
            return self.connection
        except connector.errors.Error as e:
            logger.error(f'Error connecting to Snowflake, {e}!')
            raise

    def disconnect(self):
        """
        Closes the connection to the Snowflake account if it's currently open.
        """

        if self.is_connected is False:
            return
        self.connection.close()
        self.is_connected = False

    def check_connection(self) -> StatusResponse:
        """
        Checks the status of the connection to the Snowflake account.

        Returns:
            StatusResponse: An object containing the success status and an error message if an error occurs.
        """

        response = StatusResponse(False)
        need_to_close = not self.is_connected

        try:
            connection = self.connect()

            # Execute a simple query to test the connection
            with connection.cursor() as cur:
                cur.execute('select 1;')
            response.success = True
        except (connector.errors.Error, ValueError) as e:
            logger.error(f'Error connecting to Snowflake, {e}!')
            response.error_message = str(e)

        if response.success and need_to_close:
            self.disconnect()

        elif not response.success and self.is_connected:
            self.is_connected = False

        return response

    def native_query(self, query: str) -> Response:
        """
        Executes a SQL query on the Snowflake account and returns the result.

        Args:
            query (str): The SQL query to be executed.

        Returns:
            Response: A response object containing the result of the query or an error message.
        """

        need_to_close = self.is_connected is False

        connection = self.connect()
        with connection.cursor(connector.DictCursor) as cur:
            try:
                cur.execute(query)
                try:

                    try:
                        batches_iter = cur.fetch_pandas_batches()
                    except ValueError:
                        # duplicated columns raises ValueError
                        raise NotSupportedError()

                    batches = []
                    memory_estimation_check_done = False

                    for batch_df in batches_iter:
                        batches.append(batch_df)
                        # region check the size of first batch (if it is big enough) to get an estimate of the full
                        # dataset size. If i does not fit in memory - raise an error.
                        # NOTE batch size cannot be set on client side. Also, Snowflake will download
                        # 'CLIENT_PREFETCH_THREADS' count of chunks in parallel (by default 4), therefore this check
                        # can not work in some cases.
                        batches_rowcount = sum([len(x) for x in batches])
                        if memory_estimation_check_done is False and batches_rowcount > 1000:
                            memory_estimation_check_done = True
                            available_memory_kb = psutil.virtual_memory().available >> 10
                            batches_size_kb = sum([(x.memory_usage(index=True, deep=True).sum() >> 10) for x in batches])
                            total_rowcount = cur.rowcount
                            rest_rowcount = total_rowcount - batches_rowcount
                            rest_estimated_size_kb = int((rest_rowcount / batches_rowcount) * batches_size_kb)
                            if (available_memory_kb * 0.9) < rest_estimated_size_kb:
                                logger.error(
                                    'Attempt to get too large dataset:\n'
                                    f'batches_rowcount={batches_rowcount}, size_kb={batches_size_kb}\n'
                                    f'total_rowcount={total_rowcount}, estimated_size_kb={rest_estimated_size_kb}\n'
                                    f'available_memory_kb={available_memory_kb}'
                                )
                                raise MemoryError('Not enought memory')
                        # endregion
                    if len(batches) > 0:
                        response = Response(
                            RESPONSE_TYPE.TABLE,
                            pandas.concat(batches, ignore_index=True)
                        )
                    else:
                        response = Response(
                            RESPONSE_TYPE.TABLE,
                            DataFrame(
                                [],
                                columns=[x[0] for x in cur.description]
                            )
                        )
                except NotSupportedError:
                    # Fallback for CREATE/DELETE/UPDATE. These commands returns table with single column,
                    # but it cannot be retrieved as pandas DataFrame.
                    result = cur.fetchall()
                    match result:
                        case (
                            [{'number of rows inserted': affected_rows}]
                            | [{'number of rows deleted': affected_rows}]
                            | [{'number of rows updated': affected_rows, 'number of multi-joined rows updated': _}]
                        ):
                            response = Response(RESPONSE_TYPE.OK, affected_rows=affected_rows)
                        case list():
                            response = Response(
                                RESPONSE_TYPE.TABLE,
                                DataFrame(
                                    result,
                                    columns=[x[0] for x in cur.description]
                                )
                            )
                        case _:
                            # Looks like SnowFlake always returns something in response, so this is suspicious
                            logger.warning('Snowflake did not return any data in response.')
                            response = Response(RESPONSE_TYPE.OK)
            except Exception as e:
                logger.error(f"Error running query: {query} on {self.connection_data.get('database')}, {e}!")
                response = Response(
                    RESPONSE_TYPE.ERROR,
                    error_code=0,
                    error_message=str(e)
                )

        if need_to_close is True:
            self.disconnect()

        if memory_pool is not None and memory_pool.backend_name == 'jemalloc':
            # This reduce memory consumption, but will slow down next query slightly.
            # Except pool type 'jemalloc': memory consumption do not change significantly
            # and next query processing time may be even lower.
            memory_pool.release_unused()

        return response

    def query(self, query: ASTNode) -> Response:
        """
        Executes a SQL query represented by an ASTNode and retrieves the data.

        Args:
            query (ASTNode): An ASTNode representing the SQL query to be executed.

        Returns:
            Response: The response from the `native_query` method, containing the result of the SQL query execution.
        """

        query_str = self.renderer.get_string(query, with_failback=True)
        logger.debug(f"Executing SQL query: {query_str}")
        result = self.native_query(query_str)
        return self.lowercase_columns(result, query)

    def lowercase_columns(self, result, query):
        if not isinstance(query, Select) or result.data_frame is None:
            return result

        quoted_columns = []
        if query.targets is not None:
            for column in query.targets:
                if hasattr(column, 'alias') and column.alias is not None:
                    if column.alias.is_quoted[-1]:
                        quoted_columns.append(column.alias.parts[-1])
                elif isinstance(column, Identifier):
                    if column.is_quoted[-1]:
                        quoted_columns.append(column.parts[-1])

        rename_columns = {}
        for col in result.data_frame.columns:
            if col.isupper() and col not in quoted_columns:
                rename_columns[col] = col.lower()
        if rename_columns:
            result.data_frame = result.data_frame.rename(columns=rename_columns)
        return result

    def get_tables(self) -> Response:
        """
        Retrieves a list of all non-system tables and views in the current schema of the Snowflake account.

        Returns:
            Response: A response object containing the list of tables and views, formatted as per the `Response` class.
        """

        query = """
            SELECT TABLE_NAME, TABLE_SCHEMA, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')
              AND TABLE_SCHEMA = current_schema()
        """
        return self.native_query(query)

    def get_columns(self, table_name) -> Response:
        """
        Retrieves column details for a specified table in the Snowflake account.

        Args:
            table_name (str): The name of the table for which to retrieve column information.

        Returns:
            Response: A response object containing the column details, formatted as per the `Response` class.

        Raises:
            ValueError: If the 'table_name' is not a valid string.
        """

        if not table_name or not isinstance(table_name, str):
            raise ValueError("Invalid table name provided.")

        query = f"""
            SELECT
                COLUMN_NAME,
                DATA_TYPE,
                ORDINAL_POSITION,
                COLUMN_DEFAULT,
                IS_NULLABLE,
                CHARACTER_MAXIMUM_LENGTH,
                CHARACTER_OCTET_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE,
                DATETIME_PRECISION,
                CHARACTER_SET_NAME,
                COLLATION_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = '{table_name}'
              AND TABLE_SCHEMA = current_schema()
        """
        result = self.native_query(query)
        result.to_columns_table_response(map_type_fn=_map_type)

        return result
