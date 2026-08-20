from target_dynamics_v2.mappers.base_mappers import BaseMapper

class BillCommentSchemaMapper(BaseMapper):
    name = "Billlines"
    field_mappings = {
        "description": "description",
    }

    def __init__(
            self,
            record,
            sink,
            reference_data,
            existing_lines
    ) -> None:
        self.existing_lines = existing_lines
        super().__init__(record, sink, reference_data)

    def to_netsuite(self) -> dict:
        payload = {
            **self._map_internal_id(),
            "lineType": "Comment",
        }

        self._map_fields(payload)

        return payload

    def _find_existing_record(self, reference_list):
        """Finds an existing record in the reference data by matching internal.
        """

        
        existing_lines = self.existing_lines
        if found_record := next(
            (line for line in existing_lines 
            if line.get("description") == self.record.get("description")
            and line.get("lineType") == "Comment"), 
            None):
            return found_record
        
        return None
