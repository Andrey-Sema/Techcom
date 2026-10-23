-- psql quotes the password as a SQL literal, including embedded quotes.
-- Re-running initialization preserves the role and its existing grants.
SELECT 'CREATE ROLE followup_service LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'followup_service')
\gexec
ALTER ROLE followup_service LOGIN PASSWORD :'service_password';
GRANT followup_app TO followup_service;
