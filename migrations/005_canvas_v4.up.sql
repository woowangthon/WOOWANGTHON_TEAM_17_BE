ALTER TABLE `trip_canvas_documents`
  DROP CHECK `ck_trip_canvas_document_schema`,
  ADD CONSTRAINT `ck_trip_canvas_document_schema` CHECK (`schema_version` IN (3, 4));
