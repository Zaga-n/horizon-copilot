## Purpose

Provide a clear, responsive authenticated workspace for private Horizon conversations and source documents.

## ADDED Requirements

### Requirement: Google sign-in and session lifecycle
The frontend SHALL show a Google sign-in entry screen before loading protected data, send the resulting Google ID token to both APIs, and provide sign-out. It SHALL clear user-scoped cached data on sign-out or account change and require reauthentication on token expiry without automatically resubmitting a turn or upload whose outcome is unknown.

#### Scenario: Successful login
- **WHEN** a user completes Google sign-in
- **THEN** the workspace loads that owner's conversations and documents and exposes the chatbot

#### Scenario: Expired token during work
- **WHEN** a protected request is rejected for an expired identity token
- **THEN** the frontend requests sign-in and reconciles accepted work from server history/status after authentication before offering retry

#### Scenario: Account switch
- **WHEN** a signed-in user signs out and another account signs in
- **THEN** previous conversation, document, stream, and feedback data is cleared from the client

### Requirement: Responsive accessible workspace
The workspace SHALL present conversation navigation, the chat area, and a discoverable document panel. It SHALL support keyboard navigation, visible focus, named icon actions, labelled fields, readable contrast, reduced motion, and mobile layouts without horizontal page scrolling. Loading, empty, and error states SHALL provide clear next actions; status announcements SHALL avoid announcing every token.

#### Scenario: Mobile document management
- **WHEN** a user opens the workspace on a narrow viewport
- **THEN** conversation navigation and document management are accessible through drawers and the composer remains usable

#### Scenario: Keyboard navigation
- **WHEN** a keyboard user opens and closes a document or feedback dialog
- **THEN** focus enters the dialog, stays within it while open, and returns to the triggering control on close
