"""Tests for grade change detection logic."""

from unittest.mock import MagicMock, patch

import pytest

from filoutil.moodle_cabinet.course_watcher import detect_grade_changes


def is_register_total(itemname: str) -> bool:
    name = itemname.lower()
    return "register total" in name or "register(not to edit) total" in name


class TestDetectGradeChanges:
    def test_register_total_ignored_when_final_unavailable(self):
        new_grades = [
            {"id": 1, "itemname": "Register Midterm", "graderaw": 90.0, "gradeformatted": "90.00"},
            {"id": 2, "itemname": "Register Endterm", "graderaw": 89.0, "gradeformatted": "89.00"},
            {"id": 3, "itemname": "Register Term", "graderaw": 90.0, "gradeformatted": "90.00"},
            {"id": 4, "itemname": "Register Final", "graderaw": None, "gradeformatted": "-"},
            {
                "id": 5,
                "itemname": "Register(not to edit) total",
                "graderaw": 0.0,
                "gradeformatted": "0.00",
            },
        ]

        mock_db = MagicMock()
        mock_db.execute.return_value.scalars.return_value.all.return_value = []

        changes = detect_grade_changes(mock_db, 1, 1, new_grades)

        register_total_changes = [
            c for c in changes if is_register_total(c["grade_data"]["itemname"])
        ]
        assert (
            len(register_total_changes) == 0
        ), "Register Total 0.00 should be ignored when Final is unavailable"

        other_changes = [c for c in changes if not is_register_total(c["grade_data"]["itemname"])]
        assert len(other_changes) == 4

    def test_register_total_shown_when_final_available(self):
        new_grades = [
            {"id": 1, "itemname": "Register Midterm", "graderaw": 90.0, "gradeformatted": "90.00"},
            {"id": 2, "itemname": "Register Endterm", "graderaw": 89.0, "gradeformatted": "89.00"},
            {"id": 3, "itemname": "Register Term", "graderaw": 90.0, "gradeformatted": "90.00"},
            {"id": 4, "itemname": "Register Final", "graderaw": 85.0, "gradeformatted": "85.00"},
            {
                "id": 5,
                "itemname": "Register(not to edit) total",
                "graderaw": 0.0,
                "gradeformatted": "0.00",
            },
        ]

        mock_db = MagicMock()
        mock_db.execute.return_value.scalars.return_value.all.return_value = []

        changes = detect_grade_changes(mock_db, 1, 1, new_grades)

        register_total_changes = [
            c for c in changes if is_register_total(c["grade_data"]["itemname"])
        ]
        assert (
            len(register_total_changes) == 1
        ), "Register Total 0.00 should be shown when Final is available"

    def test_course_total_ignored_always(self):
        new_grades = [
            {"id": 1, "itemname": "Course total", "graderaw": 0.0, "gradeformatted": "0.00"},
            {"id": 2, "itemname": "Some Assignment", "graderaw": 95.0, "gradeformatted": "95.00"},
        ]

        mock_db = MagicMock()
        mock_db.execute.return_value.scalars.return_value.all.return_value = []

        changes = detect_grade_changes(mock_db, 1, 1, new_grades)

        course_total_changes = [
            c for c in changes if "course total" in c["grade_data"]["itemname"].lower()
        ]
        assert len(course_total_changes) == 0, "Course Total 0.00 should always be ignored"

    def test_non_zero_register_total_always_shown(self):
        new_grades = [
            {"id": 1, "itemname": "Register Final", "graderaw": None, "gradeformatted": "-"},
            {
                "id": 2,
                "itemname": "Register(not to edit) total",
                "graderaw": 89.5,
                "gradeformatted": "89.50",
            },
        ]

        mock_db = MagicMock()
        mock_db.execute.return_value.scalars.return_value.all.return_value = []

        changes = detect_grade_changes(mock_db, 1, 1, new_grades)

        register_total_changes = [
            c for c in changes if is_register_total(c["grade_data"]["itemname"])
        ]
        assert len(register_total_changes) == 1, "Non-zero Register Total should always be shown"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
