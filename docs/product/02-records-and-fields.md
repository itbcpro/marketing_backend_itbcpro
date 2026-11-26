# Module: Records, custom fields, tags, associations
Status: approved   Owner: Ahmad   Phase: 1   Source: ghl-spec section 3

## Purpose
One generic engine for standard and custom objects: typed custom fields, workspace values, tags and labelled associations.

## In scope for MVP / Out of scope
In: field definitions for contact, company and opportunity; 11 field types; workspace values; tags; labelled associations; merge-field resolver.
Out for MVP: user-defined custom objects (CRM-1.4.3 later), field-level permissions, formula fields.

## Data model
`FieldDefinition(object_type, key, label, field_type, options, folder, position, archived_at)`; `FieldValue(object_type, record_id, field, value_text, value_number, value_date, value_json)`; `WorkspaceValue(key, label, value)`; `Tag(name, color)` + `ContactTag`; `Association(from_type, from_id, to_type, to_id, label)`.
Types: text, long_text, number, money, phone, email, url, date, select, multi_select, checkbox. Allowed types are an enum, never strings in code.

## API
`/crm/fields`, `/crm/values`, `/crm/tags`, `/crm/associations` with CRUD, reorder, archive.

## Events
`crm.field.created`, `crm.field.archived`, `crm.tag.added`, `crm.tag.removed`.

## Business rules
1. Field `key` is unique per workspace and object type, lowercase snake case, immutable after creation.
2. Reserved keys (standard fields) cannot be used.
3. A field with stored values is archived, never deleted.
4. A value is validated and normalised by its field type before saving (phone to E.164, email lowercased, money with currency).
5. `{{contact.<key>}}` and `{{workspace_values.<key>}}` resolve with HTML escaping; missing values use the configured fallback.
6. Tag names are unique case-insensitively per workspace.

## Edge cases
Renaming a tag keeps its id; merging tags moves links without duplicates; select option removal keeps old values readable.

## Security notes
Templates never evaluate code; only the two namespaces above resolve.

## Acceptance criteria
- Given a number field, when a text value is saved, then validation fails with a field error.
- Given an archived field, when a contact is read, then its old value is still returned and new writes are refused.
- Given two tags differing only by case, when the second is created, then a conflict is returned.
- Given a template with an unknown key, when rendered, then the fallback text is used and no error leaks.

## Open questions
Maximum fields per workspace per plan.

## Custom fields page
`/crm/fields` manages fields for contacts, opportunities and companies: create, edit label, options and folder (key and type never change), archive, and rename a folder in one step. Folders are the folder names fields carry, so a folder appears once a field is moved into it. Merge tags for contacts and companies are shown as `{{contact.key}}`.
