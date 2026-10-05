import unittest

from exhibition_tour.storage import Database


class TourStorageTest(unittest.TestCase):
    def test_transaction_rolls_back(self):
        database = Database()
        with self.assertRaises(RuntimeError):
            with database.transaction():
                database.connection.execute(
                    "INSERT INTO participants(participant_id,display_name,role,organization_id,"
                    "active,created_at) VALUES('p1','场馆','venue','o1',1,'now')"
                )
                raise RuntimeError("停止事务")
        count = database.connection.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        self.assertEqual(0, count)
        database.close()

    def test_foreign_keys_enabled(self):
        database = Database()
        enabled = database.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(1, enabled)
        database.close()

    def test_holds_enforce_one_row_per_segment_resource(self):
        database = Database()
        database.connection.execute(
            "INSERT INTO participants(participant_id,display_name,role,organization_id,active,created_at) "
            "VALUES('p1','场馆','venue','o1',1,'now')")
        database.connection.execute(
            "INSERT INTO resources(resource_id,kind,owner_id,label,capabilities_json,created_at) "
            "VALUES('r1','vehicle','p1','车','{}','now')")
        for index in range(2):
            database.connection.execute(
                "INSERT INTO resource_holds(hold_id,resource_id,plan_id,version,segment_id,"
                "start_at,end_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"h{index}", "r1", f"plan{index}", 1, f"seg{index}", "t0", "t1", "held", "now"))
        with self.assertRaises(Exception):
            database.connection.execute(
                "INSERT INTO resource_holds(hold_id,resource_id,plan_id,version,segment_id,"
                "start_at,end_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("hx", "r1", "planx", 1, "seg0", "t0", "t1", "held", "now"))
        database.close()


if __name__ == "__main__":
    unittest.main()
