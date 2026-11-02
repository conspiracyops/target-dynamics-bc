from typing import List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.constants import countries
from target_dynamics_v2.mappers.vendor_schema_mapper import VendorSchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkBatchUpsert

class VendorSink(DynamicsBaseBatchSinkBatchUpsert):
    name = "Vendors"
    record_type = "Vendors"

    def handle_non_atomic_batch_response(self, responses: List[dict], records: List[dict], raw_records: List[dict]) -> dict:
        for index, response in enumerate(responses):
            if response.get("status", 0) < 400:
                continue
            if not DynamicsClient.is_gen_bus_posting_group_error(response.get("body", {}).get("error")):
                continue

            record = records[index]
            raw_record = raw_records[record["raw_record_index"]]
            retry_response = self._retry_create_via_classic_odata(record, raw_record)
            if retry_response is not None:
                responses[index] = retry_response

        return super().handle_non_atomic_batch_response(responses, records, raw_records)

    COUNTRY_ALIASES = {"UK": "GB", "UAE": "AE"}

    def _resolve_vendor_country(self, vendor_payload: dict):
        country = (vendor_payload.get("country") or "").strip()
        if not country:
            return None
        country = self.COUNTRY_ALIASES.get(country.upper(), country)
        if len(country) != 2:
            country = countries.get(country)
        return country.upper() if country else None

    def _pick_posting_groups(self, company: dict, vendor_payload: dict, currency_code):
        """Chooses the local or foreign posting group set configured for this company.

        Country decides when present; otherwise the vendor currency is compared to the
        company currency. Returns None when it can't be decided, so the original error
        stays visible instead of posting the vendor with the wrong tax setup.
        """
        defaults = (self._target.config.get("vendor_posting_defaults") or {}).get(company["name"])
        if not defaults:
            return None

        vendor_country = self._resolve_vendor_country(vendor_payload)
        if vendor_country:
            is_local = vendor_country == defaults.get("country")
        elif currency_code:
            is_local = currency_code == defaults.get("currency")
        else:
            return None

        return defaults.get("local" if is_local else "foreign")

    def _retry_create_via_classic_odata(self, record: dict, raw_record: dict):
        company_id = record["state_fields"]["companyId"]
        company = next(
            (c for c in self.reference_data.get("companies", []) if c["id"] == company_id),
            None
        )
        if not company:
            return None

        vendor_payload = record["records"][0]["payload"]
        currency_code = self._get_output_currency(raw_record, vendor_payload, company)

        posting_groups = self._pick_posting_groups(company, vendor_payload, currency_code)
        if not posting_groups:
            return None

        classic_payload = DynamicsClient.build_classic_vendor_payload(vendor_payload, posting_groups, currency_code)
        self.logger.info(
            f"Retrying vendor '{vendor_payload.get('displayName')}' via classic OData with posting groups {posting_groups}"
        )

        try:
            return self.dynamics_client.create_vendor_via_classic_odata(company_id, company["name"], classic_payload)
        except Exception as exc:
            self.logger.error(f"Classic OData vendor creation retry failed: {exc}")
            return None

    def _get_output_currency(self, record: dict, payload: dict, company: dict):
        if currency_id := payload.get("currencyId"):
            found_currency = next(
                (currency for currency in company.get("currencies", []) if currency.get("id") == currency_id),
                None
            )
            if found_currency:
                return found_currency.get("code") or found_currency.get("displayName") or found_currency.get("id")

        if currency_code := payload.get("currencyCode"):
            return currency_code

        return record.get("currency") or record.get("currencyName") or record.get("currencyId")

    def preprocess_batch(self, records: List[dict]):
        # fetch reference data related to existing vendors
        filter_mappings = [
            {"field_from": "id", "field_to": "id", "should_quote": False},
            {"field_from": "vendorNumber", "field_to": "number", "should_quote": True}
        ]

        existing_company_vendors = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            filter_mappings,
            expand="defaultDimensions"
        )

        self.reference_data = {**self._target.reference_data, self.name: existing_company_vendors}

    def process_batch_record(self, record: dict) -> dict:
        # perform the mapping
        mapped_record = VendorSchemaMapper(record, self, self.reference_data)
        payload = mapped_record.to_dynamics()

        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, mapped_record.company["id"], payload.get("id"))

        default_dimensions_requests = []
        if mapped_record.existing_record and payload.get("defaultDimensions"):
            default_dimensions = payload.pop("defaultDimensions")
            default_dimensions_requests = DynamicsClient.create_default_dimensions_requests(
                self.record_type,
                mapped_record.company["id"],
                payload["id"],
                default_dimensions
            )

        records = [{"payload": payload, "request_params": request_params }]
        records += default_dimensions_requests

        return {
            "records": records,
            "state_fields": {
                "name": record.get("vendorName"),
                "mapField": self._get_output_currency(record, payload, mapped_record.company),
                "companyId": mapped_record.company["id"],
            },
        }
