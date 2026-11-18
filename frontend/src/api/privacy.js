import {request,post} from './client.js';
export const privacy={
  budget:()=>request('/privacy/budget'),
  list:()=>request('/privacy/releases'),
  get:id=>request(`/privacy/releases/${id}`),
  create:x=>post('/privacy/releases',x),
  retry:id=>post(`/privacy/releases/${id}/retry`),
  publish:id=>post(`/privacy/releases/${id}/publish`),
  revoke:id=>post(`/privacy/releases/${id}/revoke`),
  exportUrl:id=>`/privacy/releases/${id}/export`
};
