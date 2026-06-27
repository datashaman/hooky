const assert = require('node:assert/strict');
const test = require('node:test');
const { add } = require('../../../src/calculator');

test('add returns the sum of two numbers', () => {
  assert.equal(add(1, 2), 4);
});

