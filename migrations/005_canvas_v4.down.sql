-- This rollback succeeds only after all v4 documents have been explicitly migrated.
ALTER TABLE `trip_canvas_documents`
  DROP CHECK `ck_trip_canvas_document_schema`,
  ADD CONSTRAINT `ck_trip_canvas_document_schema` CHECK (`schema_version` = 3);
