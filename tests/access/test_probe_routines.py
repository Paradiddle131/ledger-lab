import pytest
from access.review import review
from tests.access.test_review import planted, docs, config  # noqa: F401

ROUTINES = """
resource "google_bigquery_dataset_access" "aggregates_routines_read_source" {
  for_each   = local.aggregate_sources
  dataset_id = google_bigquery_dataset.warehouse[each.value].dataset_id
  dataset {
    dataset {
      project_id = var.project
      dataset_id = google_bigquery_dataset.warehouse[each.key].dataset_id
    }
    target_types = ["ROUTINES"]
  }
}"""

def test_routines_authorization_is_refused(docs, config):  # noqa: F811
    with pytest.raises(ValueError, match="refuses the configuration"):
        review(docs, planted(config, ROUTINES))
