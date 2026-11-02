from typing import List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.constants import COUNTRY_ALIASES, countries
from target_dynamics_v2.mappers.vendor_schema_mapper import VendorSchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkBatchUpsert

class VendorSink(DynamicsBaseBatchSinkBatchUpsert):
    name = "Vendors"
    record_type = "Vendors"

    def make_batch_request(self, records: List[dict], transaction_type: str = "non_atomic"):
        """New vendors of companies that have vendor_posting_defaults are created through
        the classic OData service from the start (the standard API can't set posting
        groups, and some companies silently end up with empty ones). Everything else goes
        through the regular $batch; responses keep the order of `records`."""
        if not any(record.get("classic") for record in records):
            return super().make_batch_request(records, transaction_type)

        responses = [None] * len(records)
        standard = [(index, record) for index, record in enumerate(records) if not record.get("classic")]
        if standard:
            standard_responses = super().make_batch_request([record for _, record in standard], transaction_type)
            for (index, _), response in zip(standard, standard_responses):
                responses[index] = response

        for index, record in enumerate(records):
            if record.get("classic"):
                responses[index] = self._create_via_classic_odata(record["classic"])

        return responses

    def handle_non_atomic_batch_response(self, responses: List[dict], records: List[dict], raw_records: List[dict]) -> dict:
        for index, response in enumerate(responses):
            if response.get("status", 0) < 400:
                continue
            if not DynamicsClient.is_gen_bus_posting_group_error(response.get("body", {}).get("error")):
                continue

            record = records[index]
            if record.get("classic"):
                continue  # already created through the classic service, nothing else to retry with

            raw_record = raw_records[record["raw_record_index"]]
            classic = self._build_classic_create(raw_record, record["records"][0]["payload"], record["state_fields"]["companyId"])
            if classic:
                self.logger.info(f"Retrying vendor '{classic['display_name']}' via classic OData")
                responses[index] = self._create_via_classic_odata(classic)

        return super().handle_non_atomic_batch_response(responses, records, raw_records)

    def _create_via_classic_odata(self, classic: dict) -> dict:
        self.logger.info(
            f"Creating vendor '{classic['display_name']}' via classic OData with posting groups {classic['posting_groups']}"
        )
        try:
            return self.dynamics_client.create_vendor_via_classic_odata(
                classic["company_id"], classic["company_name"], classic["payload"]
            )
        except Exception as exc:
            self.logger.error(f"Classic OData vendor creation failed: {exc}")
            return {"status": 500, "body": {"error": {"message": f"Classic OData vendor creation failed: {exc}"}}}

    def _resolve_vendor_country(self, vendor_payload: dict):
        country = (vendor_payload.get("country") or "").strip()
        if not country:
            return None
        country = COUNTRY_ALIASES.get(country.upper(), country)
        if len(country) != 2:
            country = countries.get(country)
        return country.upper() if country else None

    def _pick_posting_groups(self, company: dict, vendor_payload: dict, currency_code):
        """Chooses the local or foreign posting group set configured for this company.

        Country decides when present; otherwise the vendor currency is compared to the
        company currency. Returns None when it can't be decided, so the vendor falls back
        to the regular flow instead of being posted with the wrong tax setup.
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

    def _build_classic_create(self, raw_record: dict, vendor_payload: dict, company_id: str):
        company = next(
            (c for c in self.reference_data.get("companies", []) if c["id"] == company_id),
            None
        )
        if not company:
            return None

        currency_code = self._get_output_currency(raw_record, vendor_payload, company)
        posting_groups = self._pick_posting_groups(company, vendor_payload, currency_code)
        if not posting_groups:
            return None

        return {
            "company_id": company_id,
            "company_name": company["name"],
            "display_name": vendor_payload.get("displayName"),
            "posting_groups": posting_groups,
            "payload": DynamicsClient.build_classic_vendor_payload(vendor_payload, posting_groups, currency_code),
        }

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

        result = {
            "records": records,
            "state_fields": {
                "name": record.get("vendorName"),
                "mapField": self._get_output_currency(record, payload, mapped_record.company),
                "companyId": mapped_record.company["id"],
            },
        }

        if not mapped_record.existing_record:
            if classic := self._build_classic_create(record, payload, mapped_record.company["id"]):
                result["classic"] = classic

        return result
