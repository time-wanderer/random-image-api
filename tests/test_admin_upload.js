'use strict';
const assert = require('node:assert/strict');
const upload = require('../app/admin_upload.js');
const file = (name, type, size) => ({name, type, size});
const limit = 10 * 1024 * 1024;

assert.equal(upload.validateImages([file('ok.JPG', 'image/jpeg', limit)], limit).ok, true);
assert.equal(upload.validateImages([file('archive.zip', 'application/zip', 1)], limit).ok, false);
assert.equal(upload.validateImages([file('fake.jpg', 'image/png', 1)], limit).ok, false);
assert.equal(upload.validateImages([file('large.webp', 'image/webp', limit + 1)], limit).ok, false);
assert.equal(upload.validateArchive([file('ok.tar.gz', 'application/gzip', limit)], limit).ok, true);
assert.equal(upload.validateArchive([file('bad.tar', 'application/x-tar', 1)], limit).ok, false);
assert.equal(upload.validateArchive([file('large.tgz', 'application/gzip', limit + 1)], limit).ok, false);
assert.match(upload.validateArchive([file('large.zip', 'application/zip', limit + 1)], limit).message, /CLI/);
console.log('admin_upload.js validation tests passed');
