# utils/table_migrator.py
from typing import List, Dict, Tuple
from logger_config import log_action

class LegacyTableMigrator:
    EXPECTED_COLUMNS = {
        'RELATIVE_PATH': {'type': 'VARCHAR'},
        'PAGE_NUMBER': {'type': 'NUMBER'},
        'CHUNK': {'type': 'VARCHAR'},
        'CHUNK_ID': {'type': 'VARCHAR'},
        'CHUNK_TYPE': {'type': 'VARCHAR DEFAULT \'STANDARD\''},
        'CHUNK_REF': {'type': 'VARCHAR'},
        'LINK_BLOCK': {'type': 'VARCHAR'},
        'CHUNK_METADATA': {'type': 'VARIANT'},
    }

    @staticmethod
    def migrate_table(session, db: str, schema: str, table_name: str) -> Dict:
        result = {'success': False, 'columns_added': [], 'errors': []}
        full_table = f'"{db}"."{schema}"."{table_name}"'
        try:
            res = session.sql(f'DESCRIBE TABLE {full_table}').collect()
            existing_cols = {row['name'].upper() for row in res}
            
            missing = [col for col in LegacyTableMigrator.EXPECTED_COLUMNS.keys() if col.upper() not in existing_cols]
            
            for col_name in missing:
                col_type = LegacyTableMigrator.EXPECTED_COLUMNS[col_name]['type']
                session.sql(f'ALTER TABLE {full_table} ADD COLUMN {col_name} {col_type}').collect()
                result['columns_added'].append(col_name)
            
            result['success'] = True
        except Exception as e:
            if "does not exist" in str(e).lower():
                result['success'] = True
            else:
                result['errors'].append(str(e))
                log_action("TABLE_MIGRATION_ERROR", {"table": full_table, "error": str(e)}, level="ERROR")
        return result
