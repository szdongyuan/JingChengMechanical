# Password Reset Script Design

## Goal

Create a standalone `password_reset.py` program that resets the original SQLite database to contain a known `admin/admin` administrator account for first-time customer installation recovery.

## Context

- This script is intentionally separate from the main program.
- It is a one-time-use support tool with no UI.
- It must work against an existing database file named `jingchen.db` located in the same folder as the script at runtime.
- It must not create or migrate database schema.

## Approaches

### Recommended: Self-contained standalone script

- Implement `password_reset.py` as a single file using `sqlite3`.
- Duplicate the current login hash algorithm exactly, including the MAC-address input and SHA-1 format.
- Do not import PyQt or the application UI modules.

Why this is preferred:
- stays independent from the main program as requested
- can be copied next to a customer database and run once
- avoids UI/runtime dependency issues in a support scenario

### Alternative: Import the current hash function from `ui.modules.login`

- Lower code duplication.
- Rejected because it makes a no-UI support script depend on PyQt/UI imports and the full app module layout.

### Alternative: Add a password-reset feature into the main app

- Rejected because the user explicitly wants a separate program and no UI.

## Design

### File layout

- Create one new file: `password_reset.py`

### Runtime behavior

When executed:

1. Resolve the database path as `Path(__file__).resolve().with_name("jingchen.db")`.
2. If the database file does not exist, print an error and exit non-zero.
3. Open the SQLite database directly with `sqlite3`.
4. Verify that `users_table` exists.
5. Verify that `users_table` contains the expected current columns:
   - `user_name`
   - `password`
   - `access_level`
   - `user_created_time`
   - `user_updated_time`
6. If `users_table` is missing or incompatible, print an error and exit non-zero.
7. In one transaction:
   - delete every row whose `access_level = 'Admin'` and `user_name != 'admin'`
   - if `user_name = 'admin'` exists, update it so:
     - `password = encrypt_password("admin", "admin")`
     - `access_level = "Admin"`
     - `user_updated_time = now_str()`
     - `user_created_time` is preserved unless blank/null, in which case fill it with `now_str()`
   - otherwise insert a new `admin` row with:
     - `user_name = "admin"`
     - `password = encrypt_password("admin", "admin")`
     - `access_level = "Admin"`
     - `user_created_time = now_str()`
     - `user_updated_time = now_str()`
8. Print a success message and exit zero.

### Hash compatibility

The script must reproduce the current login hash exactly:

- use the same MAC-address acquisition logic as the current application
- hash `get_mac_address() + user_name + password`
- use SHA-1 hex digest in lowercase

### Timestamp compatibility

The script must reproduce the current user timestamp format exactly:

- timezone UTC+8
- format `%Y-%m-%d %H:%M:%S`

## Error handling

The script must fail clearly, print a short error message, and exit non-zero when:

- `jingchen.db` is missing
- the database cannot be opened
- `users_table` is missing
- `users_table` lacks the expected current columns
- the reset transaction fails

## Testing

Add automated tests that prove:

- the standalone hash function matches the current login hash behavior
- the script errors when the database file is missing
- the script errors when `users_table` is missing
- the script inserts `admin/admin` into an empty valid `users_table`
- the script overwrites an existing `admin` row to the expected admin state
- the script deletes other admin rows while preserving non-admin users

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: `users_table` schema, login password hash behavior, admin account semantics
- Allowed breakage: none
- Migration strategy: none; data update only
