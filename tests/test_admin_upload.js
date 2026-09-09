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
const oversizedImage = upload.validateImages([file('large.webp', 'image/webp', limit + 1)], limit).message;
const oversizedArchive = upload.validateArchive([file('large.zip', 'application/zip', limit + 1)], limit).message;
assert.match(oversizedImage, /超过网页单文件上限 10\.00 MiB/);
assert.match(oversizedArchive, /超过网页上限 10\.00 MiB/);
assert.match(oversizedImage, /请减小文件或使用命令行导入/);
assert.match(oversizedArchive, /请减小文件或使用命令行导入/);
const source = require('node:fs').readFileSync(require.resolve('../app/admin_upload.js'), 'utf8');
for (const internal of ['UPLOAD_TMP_DIR', '进程内存', 'TTL', '浏览器', '边缘临时存储', 'Cloudflare', '2.56 GiB', '代理限制', 'CLI']) {
  assert.equal(source.includes(internal), false, `JS 不应包含内部词句：${internal}`);
}
assert.match(source, /可能超过上传链路限制，请减小文件或使用命令行导入/);
assert.match(source, /正在安全校验并生成预览/);
assert.equal(source.includes('服务器正在'), false);
console.log('admin_upload.js validation tests passed');
