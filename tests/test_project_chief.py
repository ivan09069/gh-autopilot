import unittest

from project_chief import DOCTRINE, Task, build_plan, next_state, priority_score, requires_human


class ProjectChiefTests(unittest.TestCase):
    def test_priority_prefers_impact_and_urgency(self):
        high = Task("A", "high", impact=5, urgency=5, effort=5, status="buildable")
        easy = Task("B", "easy", impact=2, urgency=2, effort=1, status="buildable")
        self.assertGreater(priority_score(high), priority_score(easy))

    def test_blocked_task_never_enters_execution_queue(self):
        plan = build_plan([Task("A", "blocked", blocked=True, status="blocked")])
        self.assertEqual(plan["execution_groups"], [])
        self.assertEqual(plan["blocked_queue"][0]["task_id"], "A")

    def test_human_gate_is_separated(self):
        task = Task(
            "A",
            "deploy",
            status="review",
            requested_actions=("production_deploy",),
        )
        self.assertTrue(requires_human(task))
        plan = build_plan([task])
        self.assertEqual(plan["execution_groups"], [])
        self.assertEqual(plan["human_approval_queue"][0]["gates"], ["production_deploy"])

    def test_finite_release_packet_progression(self):
        self.assertEqual(next_state(Task("A", "idea")), "shaped")
        self.assertEqual(next_state(Task("A", "work", status="building")), "review")
        self.assertEqual(next_state(Task("A", "review", status="review")), "released")
        self.assertEqual(next_state(Task("A", "released", status="released")), "monitoring")

    def test_group_limit_and_deterministic_order(self):
        tasks = [
            Task("C", "third", group="g", status="buildable", impact=1, urgency=1),
            Task("A", "first", group="g", status="buildable", impact=5, urgency=5),
            Task("B", "second", group="g", status="buildable", impact=4, urgency=4),
        ]
        plan = build_plan(tasks, max_per_group=2)
        packets = plan["execution_groups"][0]["release_packets"]
        self.assertEqual([p["task_id"] for p in packets], ["A", "B"])

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            Task.from_mapping({"task_id": "A", "title": "bad", "status": "finished-forever"})

    def test_doctrine_is_part_of_every_plan(self):
        plan = build_plan([])
        self.assertEqual(plan["doctrine"]["motto"], "Built for JIT. Built by JIT. Built with JIT.")
        self.assertEqual(
            plan["doctrine"]["primary_metric"],
            "principal attention saved per verified release packet",
        )
        self.assertIn("no_tunnel_vision", plan["doctrine"]["principles"])
        self.assertEqual(plan["doctrine"]["motto"], DOCTRINE["motto"])


if __name__ == "__main__":
    unittest.main()
