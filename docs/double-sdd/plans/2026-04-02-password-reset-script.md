# Password Reset Script Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `password_reset.py` program that resets an existing `jingchen.db` beside the script to a known `admin/admin` administrator account without changing schema.

**Architecture:** Implement one self-contained root-level script using `sqlite3`, local hash/timestamp helpers, and explicit `users_table` validation. Cover it with focused tests that exercise both success and error cases without introducing UI or ORM dependencies into the tool itself.

**Tech Stack:** Python, sqlite3, pytest

---

### Task 1: Implement the standalone password reset script

**Files:**
- Create: `password_reset.py`
- Create: `tests/test_password_reset.py`

- [ ] **Step 1: Write the failing tests**

Add tests that cover:
- hash compatibility with the current login algorithm
- error when the DB file is missing
- error when `users_table` is missing
- insert of `admin/admin` into an empty valid `users_table`
- overwrite of an existing `admin` row
- deletion of other admin rows while preserving non-admin users

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_password_reset.py -q`

Expected: FAIL before implementation.

- [ ] **Step 3: Write the minimal standalone implementation**

Implement `password_reset.py` with:
- same-folder `jingchen.db` resolution
- exact login-compatible password hashing
- exact timestamp formatting
- `users_table` existence/schema validation
- transactional overwrite/insert behavior for the `admin` account
- non-zero exits and short error messages on failure

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_password_reset.py -q`

Expected: PASS

- [ ] **Step 5: Run broader verification**

Run: `pytest tests/test_password_reset.py tests/test_db_connection_service.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add password_reset.py tests/test_password_reset.py
git commit -m "feat: add standalone password reset script"
```
