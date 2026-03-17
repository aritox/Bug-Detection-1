import unittest

from compression.compress_pr_diff import compress_pr_diff, parse_unified_diff


SMALL_PR_DIFF = """--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-Old intro
+New intro
diff --git a/app.py b/app.py
index 123..456 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 import os
+import sys
 print("hello")
"""


LARGE_PR_DIFF = """diff --git a/src/main.py b/src/main.py
index 111..112 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,22 @@
 def run():
-    return "old"
+    value = "new"
+    line01 = 1
+    line02 = 2
+    line03 = 3
+    line04 = 4
+    line05 = 5
+    line06 = 6
+    line07 = 7
+    line08 = 8
+    line09 = 9
+    line10 = 10
+    line11 = 11
+    line12 = 12
+    line13 = 13
+    line14 = 14
+    line15 = 15
+    line16 = 16
+    line17 = 17
+    line18 = 18
+    return value
diff --git a/src/util.js b/src/util.js
index 121..122 100644
--- a/src/util.js
+++ b/src/util.js
@@ -1,2 +1,20 @@
 export function helper() {
-  return 1
+  const x01 = 1
+  const x02 = 2
+  const x03 = 3
+  const x04 = 4
+  const x05 = 5
+  const x06 = 6
+  const x07 = 7
+  const x08 = 8
+  const x09 = 9
+  const x10 = 10
+  const x11 = 11
+  const x12 = 12
+  const x13 = 13
+  const x14 = 14
+  const x15 = 15
+  const x16 = 16
+  return x16
 }
diff --git a/docs/notes.md b/docs/notes.md
index 131..132 100644
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -1 +1,8 @@
-old
+new
+line2
+line3
+line4
+line5
+line6
+line7
+line8
diff --git a/old_name.py b/new_name.py
similarity index 92%
rename from old_name.py
rename to new_name.py
@@ -1,2 +1,4 @@
 def renamed():
-    return 1
+    value = 2
+    value += 1
+    return value
diff --git a/dist/bundle.js b/dist/bundle.js
index 200..201 100644
--- a/dist/bundle.js
+++ b/dist/bundle.js
@@ -1 +1 @@
-minified_old
+minified_new
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..1111111
Binary files /dev/null and b/assets/logo.png differ
"""


DELETIONS_DIFF = """diff --git a/src/cleanup.py b/src/cleanup.py
index 111..222 100644
--- a/src/cleanup.py
+++ b/src/cleanup.py
@@ -1,4 +1,2 @@
-import os
-import sys
 keep = True
@@ -10,2 +8,3 @@
-old_value = 1
+new_value = 2
+flag = True
diff --git a/src/remove_lines.py b/src/remove_lines.py
index 333..444 100644
--- a/src/remove_lines.py
+++ b/src/remove_lines.py
@@ -1,2 +1,0 @@
-old_a
-old_b
diff --git a/src/dead.py b/src/dead.py
deleted file mode 100644
index 555..000 100644
--- a/src/dead.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""


class TestCompressPRDiff(unittest.TestCase):
    def test_small_pr_includes_all_and_handles_missing_diff_header(self):
        payload = compress_pr_diff(SMALL_PR_DIFF, max_tokens=3000, buffer_tokens=200)

        included_files = [item["file"] for item in payload["included_patches"]]
        self.assertIn("README.md", included_files)
        self.assertIn("app.py", included_files)
        self.assertEqual(payload["other_modified_files"], [])
        self.assertEqual(payload["deleted_files"], [])

    def test_large_pr_budget_language_priority_and_skip_rules(self):
        parsed = parse_unified_diff(LARGE_PR_DIFF)
        self.assertTrue(any(p.is_rename and p.filename() == "new_name.py" for p in parsed))

        payload = compress_pr_diff(
            LARGE_PR_DIFF,
            max_tokens=180,
            buffer_tokens=40,
            other_files_budget=200,
            deleted_files_budget=200,
        )

        included_files = [item["file"] for item in payload["included_patches"]]
        other_files = payload["other_modified_files"]
        all_visible = set(included_files + other_files + payload["deleted_files"])

        self.assertTrue(included_files, "Expected at least one included patch under a small budget")
        self.assertTrue(included_files[0].endswith(".py"), "Expected language priority to prefer .py")
        self.assertIn("new_name.py", all_visible)
        self.assertNotIn("dist/bundle.js", all_visible)
        self.assertNotIn("assets/logo.png", all_visible)
        self.assertGreater(len(other_files), 0, "Expected overflow files to be listed in other_modified_files")

    def test_deletion_only_hunks_are_removed_and_deleted_files_listed(self):
        payload = compress_pr_diff(DELETIONS_DIFF, max_tokens=3000, buffer_tokens=200)

        included_by_file = {item["file"]: item for item in payload["included_patches"]}
        self.assertIn("src/cleanup.py", included_by_file)
        self.assertIn("src/remove_lines.py", payload["other_modified_files"])
        self.assertIn("src/dead.py", payload["deleted_files"])

        cleanup_patch = included_by_file["src/cleanup.py"]["patch"]
        self.assertIn("@@ -10,2 +8,3 @@", cleanup_patch)
        self.assertIn("+new_value = 2", cleanup_patch)
        self.assertNotIn("-import os", cleanup_patch)
        self.assertNotIn("-import sys", cleanup_patch)


if __name__ == "__main__":
    unittest.main()
