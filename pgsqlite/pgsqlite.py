from sqlite_utils import Database
from typing import List, Any, Dict, Union, Optional
from sqlite_utils.db import Table
import datetime
import sqlite3
import psycopg
from psycopg.rows import dict_row
from pprint import pprint
from psycopg.sql import SQL, Identifier, Literal
import sys
import logging
import structlog
import argparse
import asyncio

_IGNORE_CHECKS=True
_IGNORE_TRIGGERS=True
_IGNORE_VIEWS=True

logger = structlog.get_logger(__name__)

class PGSqlite(object):

    def remap_column_type(self, column_type: str) -> str:
        if "STRING" in column_type:
            return "TEXT"
        elif "NVARCHAR" in column_type:
            return column_type.replace("NVARCHAR", "VARCHAR")
        elif "DATETIME" in column_type:
            return "TIMESTAMP"
        elif "BLOB" in column_type:
            return "BYTEA"
        return column_type

    def boolean_transformer(self, val: Any, nullable: bool) -> Union[bool, None]:
        if nullable and not val:
            return None
        if not nullable and not val:
            raise Exception("Value is None but column is not nullable")

        if val == 1 or val.lower() == "true":
            return "TRUE"
        return "FALSE"



    def __init__(self, sqlite_filename: str, pg_conninfo: str, show_sample_data: bool = False) -> None:
        self.sqlite_filename = sqlite_filename
        self.pg_conninfo = pg_conninfo
        self.tables_sql = []
        self.fks_sql = []
        self.indexes_sql = []
        self.checks_sql_by_table = {}
        self.summary = {}
        self.summary["tables"] = {}
        self.summary["tables"]["columns"] = {}
        self.summary["tables"]["pks"] = {}
        self.summary["tables"]["fks"] = {}
        self.summary["tables"]["checks"] = {}
        self.summary["tables"]["data"] = {}
        self.summary["tables"]["indexes"] = {}
        self.summary["views"] = {}
        self.summary["triggers"] = {}
        self.transformers = {}
        self.transformers['BOOLEAN'] = self.boolean_transformer
        self.show_sample_data = show_sample_data

    def get_table_sql(self, table: Table) -> SQL:
        create_sql = SQL("CREATE TABLE {table_name} (").format(table_name=Identifier(table.name))
        columns_sql = []
        # columns are sorted by column id, so they are created in the "correct" order for any later INSERTS that use the order from, eg, sqlite3.iterdump()
        for column in table.columns:
            column_type = self.remap_column_type(column.type)
            column_sql = SQL("    {name} " + column_type).format(name=Identifier(column.name))
            if column.notnull:
                column_sql = SQL(" ").join([column_sql, SQL("NOT NULL")])
            if column.default_value:
                column_sql = SQL(" ").join([column_sql, SQL("DEFAULT {default_value}").format(default_value=Literal(column.default_value))])

            columns_sql.append(column_sql)
        self.summary["tables"]["columns"][table.name] = {}
        self.summary["tables"]["columns"][table.name]["status"] = "PREPARED"
        self.summary["tables"]["columns"][table.name]["count"] = len(table.columns)
        all_column_sql = SQL(",\n").join(columns_sql)

        # sqlite appears to generate PK names by splitting on the CamelCasing for the first word, contactting, and prefixing with PK_
        # So let's do something similar
        if table.pks and not table.use_rowid:
            all_column_sql = all_column_sql + SQL(",\n")
            pk_name = "PK_" + ''.join(table.pks)
            pk_sql = SQL("    CONSTRAINT {pk_name} PRIMARY KEY ({pks})").format(
                    table_name=Identifier(table.name), pk_name=Identifier(pk_name), pks=SQL(", ").join([Identifier(t) for t in table.pks]))
            all_column_sql = SQL("    ").join([all_column_sql, pk_sql])
        self.summary["tables"]["pks"][table.name] = {}
        self.summary["tables"]["pks"][table.name]["status"] = "PREPARED"
        self.summary["tables"]["pks"][table.name]["count"] = len(table.pks)


        self.summary["tables"]["checks"][table.name] = {}
        if self.checks_sql_by_table[table.name] and not _IGNORE_CHECKS:
            all_column_sql = all_column_sql + SQL(",\n")
            check_sql = SQL(",\n").join(self.checks_sql_by_table[table.name])
            all_column_sql = SQL("").join([all_column_sql, check_sql])
            self.summary["tables"]["checks"][table.name]["status"] = "PREPARED"
        else:
            self.summary["tables"]["checks"][table.name]["status"] = "IGNORED"
        self.summary["tables"]["checks"][table.name]["count"] = len(self.checks_sql_by_table[table.name])

        create_sql = SQL("\n").join([create_sql, all_column_sql, SQL(");")])

        return create_sql


    def get_fk_sql(self, table: Table) -> SQL:
        sql = []
        # create the foreign keys after the tables to avoid having to figure out the dep graph
        for fk in table.foreign_keys:
            fk_name = "FK_" + fk.other_column
            fk_sql = SQL("ALTER TABLE {table_name} ADD CONSTRAINT {key_name}  FOREIGN KEY ({column}) REFERENCES {other_table} ({other_column})").format(table_name=Identifier(table.name),
                column=Identifier(fk.column), key_name=Identifier(fk_name), other_table=Identifier(fk.other_table), other_column=Identifier(fk.other_column))
            sql.append(fk_sql)
        self.summary["tables"]["fks"][table.name] = {}
        self.summary["tables"]["fks"][table.name]["status"] = "PREPARED"
        self.summary["tables"]["fks"][table.name]["count"] = len(table.foreign_keys)
        return sql

    def get_index_sql(self, table: Table) -> SQL:
        sql = []
        for index in table.xindexes:
            col_sql = []
            for col in index.columns:
                if not col.name:
                    continue
                order = "ASC"
                if col.desc:
                    order="DESC"
                col_sql.append(SQL("{name} {sort_order}").format(name=Identifier(col.name), sort_order=SQL(order)))

            index_sql = SQL("CREATE INDEX {index_name} ON {table_name} ({columns})").format(index_name = Identifier(index.name),
                table_name=Identifier(table.name), columns=SQL(",").join(col_sql))
            sql.append(index_sql)
        self.summary["tables"]["indexes"][table.name] = {}
        self.summary["tables"]["indexes"][table.name]["status"] = "PREPARED"
        self.summary["tables"]["indexes"][table.name]["count"] = len(table.xindexes)
        return sql



    def _drop_tables(self):
        db = Database(self.sqlite_filename)
        with psycopg.connect(conninfo=self.pg_conninfo) as conn:
            with conn.cursor() as cur:
                for table in db.tables:
                    cur.execute(SQL("DROP TABLE IF EXISTS {table_name} CASCADE;").format(table_name=Identifier(table.name)))


    def get_all_tables_in_postgres(self) -> Optional[List[Any]]:
        tables_in_postgres = []
        with psycopg.connect(conninfo=self.pg_conninfo,  row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(SQL("""
                    SELECT
                        table_name, column_name, ordinal_position, is_nullable, data_type
                    FROM
                        information_schema.columns
                    WHERE
                        table_name
                    IN (
                        SELECT
                            table_name
                        FROM
                            information_schema.tables
                        WHERE
                            table_type='BASE TABLE'
                        AND
                            table_schema
                        NOT IN ('pg_catalog', 'information_schema')
                        )
                    ORDER BY
                        table_name, column_name, ordinal_position; """))
                tables_in_postgres = cur.fetchall()
        return tables_in_postgres

    def check_for_matching_tables(self) -> bool:
        # TODO: implement me
        db = Database(self.sqlite_filename)
        tables_in_postgres = self.get_all_tables_in_postgres()
        return False

    def load_schema(self, drop_existing_postgres_tables: bool = False) -> None:
        db = Database(self.sqlite_filename)
        if drop_existing_postgres_tables:
            self._drop_tables()

        self.checks_sql_by_table = self.get_check_constraints()
        for table in db.tables:
            if table.name == "sqlite_sequence":
                logger.debug("sqlite_sequence table found.")
                continue
            self.tables_sql.append(self.get_table_sql(table))
            self.fks_sql.extend(self.get_fk_sql(table))
            self.indexes_sql.extend(self.get_index_sql(table))

        if not _IGNORE_VIEWS:
            logger.debug("Ignoring views", db_filename=self.sqlite_filename)
            for view in db.views:
                # there's a bug here in the sqlite_utils library where this fails
                logger.debug(f"DB view: {view}", view=view)
                self.summary["views"][view.name] = {}
                self.summary["views"][view.name]["status"] = "IGNORED"
        if not _IGNORE_TRIGGERS:
            logger.debug("Ignoring views")
            for trigger in db.triggers:
                logger.debug(f"DB trigger: {trigger}", trigger=trigger)
                self.summary["triggers"][trigger.name] = {}
                self.summary["triggers"][trigge.name]["status"] = "IGNORED"


    async def write_table_data(self, table):
        sl_conn = sqlite3.connect(self.sqlite_filename)
        sl_cur = sl_conn.cursor()
        logger.debug(f"Loading data into {table.name}", table=table.name)
        # Given the table name came from the SQLITE database, and we're using it
        # to read from the sqlite database, we are okay with the literal substitution here
        sl_cur.execute(f'SELECT * FROM "{table.name}"')
        nullable_column_indexes = []
        for idx, c in enumerate(table.columns):
            if not c.notnull:
                nullable_column_indexes.append(idx)

        # For any non-null column, allow convert from empty string to None
        async with await psycopg.AsyncConnection.connect(conninfo=self.pg_conninfo) as conn:
            async with conn.cursor() as pg_cur:
                async with pg_cur.copy(f'COPY "{table.name}" FROM STDIN') as copy:
                    rows_copied = 0
                    for row in sl_cur:
                        row = list(row)
                        for idx, c in enumerate(table.columns):
                            if c.type in self.transformers:
                                row[idx] = self.transformers[c.type](row[idx], not c.notnull)
                            if not c.notnull:
                                # for numeric types, we need to be we don't evaluate False on a 0
                                if row[idx] != 0 and not row[idx]:
                                    row[idx] = None

                        await copy.write_row(row)
                        rows_copied += 1
                        if rows_copied % 1000 == 0:
                            self.summary["tables"]["data"][table.name]["status"] = f"LOADED {rows_copied}"

                    self.summary["tables"]["data"][table.name]["status"] = f"LOADED {rows_copied}"
                logger.info(f"Finished loading {rows_copied} rows of data into {table.name}")

        sl_conn.close()

    def load_data_to_postgres(self):
        db = Database(self.sqlite_filename)
        sl_conn = sqlite3.connect(self.sqlite_filename)
        sl_cur = sl_conn.cursor()
        for table in db.tables:
            # Given the table name came from the SQLITE database, and we're using it
            # to read from the sqlite database, we are okay with the literal substitution here
            sl_cur.execute(f'SELECT count(*) FROM "{table.name}"')
            self.summary["tables"]["data"][table.name] = {}
            self.summary["tables"]["data"][table.name]["row_count"] = sl_cur.fetchone()[0]
            self.summary["tables"]["data"][table.name]["status"] = "PREPARED"
        sl_conn.close()

        async def load_all_data():
            await asyncio.gather(*[self.write_table_data(table) for table in db.tables])
        load_results = asyncio.run(load_all_data())

        if self.show_sample_data:
            for table in db.tables:
                with psycopg.connect(conninfo=self.pg_conninfo) as conn:
                    with conn.cursor() as cur:
                        cur.execute(f'SELECT * from "{table.name}" LIMIT 10')
                        logger.debug(f"Data in {table.name}")
                        logger.debug(cur.fetchall())

    def get_summary(self) -> Dict[str, Any]:
        return self.summary


    def get_check_constraints(self):
        sl_conn = sqlite3.connect(self.sqlite_filename)
        sl_cur = sl_conn.cursor()
        sl_cur.execute('select name, sql from sqlite_master where type="table"')
        checks = {}
        for row in sl_cur:
            checks[row[0]] = []
            transpile = ""
            for line in row[1].split('\n'):
                if "CHECK" in line:
                    start = line.index("(")
                    end = line.rindex(")")
                    sql_expr= line[start + 1:end]
                    clean_check_str = "    " + line.strip().rstrip(',')
                    checks[row[0]].append(SQL(clean_check_str))
                else:
                    transpile = transpile + "\n" + line
            transpile = transpile.replace('[', '"').replace(']', '"') # Handle SQLite table names that are [foo]
            transpile = transpile.replace('`', '"') # Handle SQLLite table names that are `foo`
        sl_conn.close()

        return checks

    def populate_postgres(self)-> None:
        with psycopg.connect(conninfo=self.pg_conninfo) as conn:
            with conn.cursor() as cur:
                for create_sql in self.tables_sql:
                    logger.debug("Running SQL:")
                    logger.debug(create_sql.as_string(conn))
                    cur.execute(create_sql)
            for table in self.summary["tables"]["columns"]:
                self.summary["tables"]["columns"][table]["status"] = "CREATED"
            for table in self.summary["tables"]["pks"]:
                self.summary["tables"]["pks"][table]["status"] = "CREATED"

        self.load_data_to_postgres()

        with psycopg.connect(conninfo=self.pg_conninfo) as conn:
            with conn.cursor() as cur:
                for fk in self.fks_sql:
                    logger.debug("Running SQL:")
                    logger.debug(fk.as_string(conn))
                    cur.execute(fk)
                for table in self.summary["tables"]["fks"]:
                    self.summary["tables"]["fks"][table]["status"] = "CREATED"

                for index in self.indexes_sql:
                    logger.debug("Running SQL:")
                    logger.debug(index.as_string(conn))
                    cur.execute(index)
                for table in self.summary["tables"]["indexes"]:
                    self.summary["tables"]["indexes"][table]["status"] = "CREATED"

                # todo: add checks, views, triggers.


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f",
        "--sqlite_filename",
        type=str,
        help="sqlite database to import",
        required=True
    )
    parser.add_argument(
        "-p",
        "--postgres_connect_url",
        type=str,
        help="Postgres URL for the database to import into",
        required=True
    )
    parser.add_argument(
        "-d",
        "--debug",
        type=bool,
        default=False,
        help="Set log level to DEBUG",
    )
    parser.add_argument(
        "--show_sample_data",
        type=bool,
        default=False,
        help="After import, show up to 10 rows of the imported data in each table.",
    )
    parser.add_argument(
        "--drop_tables",
        type=bool,
        default=False,
        help="Prior to import, drop tables in the target database that have the same name as tables in the source database",
    )
    parser.add_argument(
        "--drop_everything",
        type=bool,
        default=False,
        help="Prior to import, drop everything (tables, views, triggers, etc, etc) in the target database before the import",
    )
    parser.add_argument(
        "--drop_tables_after_import",
        type=bool,
        default=False,
        help="Drop all tables in the target database after import; useful for testing",
    )
    args = parser.parse_args()

    if args.debug:
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    else:
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    sqlite_filename = args.sqlite_filename
    pg_conninfo = args.postgres_connect_url

    loader = PGSqlite(sqlite_filename, pg_conninfo, show_sample_data=args.show_sample_data)
    loader.load_schema(drop_existing_postgres_tables=args.drop_tables)
    loader.populate_postgres()
    logger.debug(loader.get_summary())

    if args.drop_tables_after_import:
        loader._drop_tables()

