const registry = new Map();

function registerOperation(name, handler) {
  registry.set(name, {
    version: 1,
    handler,
    audit: [],
  });
}

registerOperation('add', (payload) => payload.values.reduce((total, value) => total + value, 0));

function execute(command) {
  const operation = registry.get(command.operation);
  if (!operation) {
    throw new Error(`Unknown operation: ${command.operation}`);
  }
  operation.audit.push({ at: Date.now(), command });
  return operation.handler(command.payload);
}

function add(a, b) {
  return execute({ operation: 'add', payload: { values: [a, b] } });
}

module.exports = { add, execute, registerOperation };

