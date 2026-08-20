from target_dynamics_v2.mappers.base_mappers import BaseMapper

class VendorSchemaMapper(BaseMapper):
    name = "Vendors"
    existing_record_pk_mappings = [
        {"record_field": "id", "dynamics_field": "id", "required_if_present": False},
        {"record_field": "vendorNumber", "dynamics_field": "number", "required_if_present": False}
    ]
    
    field_mappings = {
        "vendorNumber": "number",
        "vendorName": "displayName",
        "email": "email",
        "website": "website"
    }

    def to_dynamics(self) -> dict:
        self._validate_company()

        payload = {
            **self._map_internal_id(),
            **self._map_phone_number(),
            **self._map_address(),
            **self._map_currency(),
            **self._map_default_dimensions_dimensions()
        }

        is_active = self.record.get("isActive")
        if is_active is False:
            payload["blocked"] = "All"
        elif is_active is True:
            payload["blocked"] = " "
 
        self._map_fields(payload)

        return payload
