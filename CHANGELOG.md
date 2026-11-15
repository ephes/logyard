# Changelog

## Unreleased

- Reject overlapping saved siblings within collapsed dashboard rows, including
  nested rows, while keeping each row's saved layout separate from the visible grid.
- Refuse detected dashboard changes during bounded reads by comparing descriptor
  metadata and consumed length, with a documented policy for atomic replacements.
