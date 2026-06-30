export default function App() {
  return (
    <header className="header">
      <h1>todos</h1>
      <input
        className="new-todo"
        placeholder="What needs to be done?"
        aria-label="New todo"
        autoFocus
        readOnly
      />
    </header>
  );
}
