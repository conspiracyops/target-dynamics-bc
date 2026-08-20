from target_dynamics_v2.mappers.base_mappers import BaseMapper

class CustomerSchemaMapper(BaseMapper):
    name = "Customers"
    existing_record_pk_mappings = [
        {"record_field": "id", "dynamics_field": "id", "required_if_present": True},
        {"record_field": "externalId", "dynamics_field": "number", "required_if_present": False}
    ]

    field_mappings = {
        "externalId": "number",
        "companyName": "displayName",
        "email": "email",
        "website": "website",
        "taxable": "taxLiable"
    }

    def to_dynamics(self) -> dict:
        self._validate_company()

        payload = {
            **self._map_internal_id(),
            **self._map_payment_method(),
            **self._map_phone_number(),
            **self._map_address(),
            **self._map_currency(),
            **self._map_default_dimensions_dimensions(),
            "type": "Person" if self.record.get("isPerson") else "Company",
        }

        is_active = self.record.get("isActive")
        if is_active is False:
            payload["blocked"] = "All"
        elif is_active is True:
            payload["blocked"] = " "

        self._map_fields(payload)

        return payload
    
    def _map_payment_method(self):
        if self.company is None:
            return {}
        
        found = None
        if payment_method := self.record.get("paymentMethod"):
            found = next(
                (item for item in self.company.get("paymentMethods", []) if item["id"] == payment_method or item["code"] == payment_method or item["displayName"] == payment_method),
                None
            )

            if found:
                return {"paymentMethodId": found["id"]}

        return {}