# utils/__init__.py
# Marker file to make this directory a package

"""
DEVELOPER NOTICE:
-----------------
The authorization logic in `utils/auth_utils.py` relies on the Snowflake Stored Procedure 
`GET_ROLES_WITH_STAGE_ACCESS(DB, SCHEMA, STAGE)`. 

IMPORTANT QUIRK: 
This SP does NOT return a standard relational table. Instead, it returns a single 
VARIANT/JSON blob. To extract authorized roles, the response must be parsed 
as a dictionary navigating the following hierarchy:
    
    Response -> ['direct_grants'] -> ['data'] -> list of { 'grantee_name': 'ROLE_NAME', ... }

If you change the SP or attempt to loop through 'res' as standard SQL rows, 
the Gatekeeper will throw a KeyError or fail to identify 'ROLE_NAME'.
"""
