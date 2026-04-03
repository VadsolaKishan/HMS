import axios from 'axios';

type GetCacheEntry = {
  timestamp: number;
  data: unknown;
};

const GET_CACHE_TTL_MS = 60 * 1000;
const getResponseCache = new Map<string, GetCacheEntry>();

const cleanupExpiredGetCache = () => {
  const now = Date.now();
  for (const [key, entry] of getResponseCache.entries()) {
    if (now - entry.timestamp > GET_CACHE_TTL_MS) {
      getResponseCache.delete(key);
    }
  }
};

const buildCacheKey = (config: { url?: string; params?: unknown }) => {
  const tokenKey = localStorage.getItem('access_token') || 'guest';
  const paramsKey = config.params ? JSON.stringify(config.params) : '';
  return `${tokenKey}:${config.url || ''}:${paramsKey}`;
};

// Get the API base URL based on the current hostname
// If on localhost, use localhost:8000
// If on an IP address, use the same IP with :8000
const getAPIBaseURL = () => {
  // If a production API URL is provided via environment variables, use it
  if (import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL;
  }
  
  const hostname = window.location.hostname;
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return 'http://localhost:8000/api';
  }
  // For IP addresses or other hosts, use the same IP
  return `http://${hostname}:8000/api`;
};

const API_BASE_URL = getAPIBaseURL();

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor to add JWT token
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('access_token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }

    const method = (config.method || 'get').toLowerCase();

    if (method === 'get') {
      cleanupExpiredGetCache();
      const cacheKey = buildCacheKey(config);
      const cached = getResponseCache.get(cacheKey);

      if (cached) {
        config.adapter = async () => ({
          data: cached.data,
          status: 200,
          statusText: 'OK',
          headers: {},
          config,
          request: {},
        });
      }

      (config as any)._cacheKey = cacheKey;
    } else if (method === 'post' || method === 'put' || method === 'patch' || method === 'delete') {
      // Mutations may affect any list/detail response; clear cached GET data.
      getResponseCache.clear();
    }

    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => {
    const method = (response.config.method || 'get').toLowerCase();

    if (method === 'get') {
      const cacheKey = (response.config as any)._cacheKey || buildCacheKey(response.config);
      getResponseCache.set(cacheKey, {
        timestamp: Date.now(),
        data: response.data,
      });
    }

    return response;
  },
  async (error) => {
    const originalRequest = error.config;

    // If 401 error and not already retrying
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const refreshToken = localStorage.getItem('refresh_token');
        if (refreshToken) {
          const response = await axios.post(`${API_BASE_URL}/token/refresh/`, {
            refresh: refreshToken,
          });

          const { access } = response.data;
          localStorage.setItem('access_token', access);

          originalRequest.headers.Authorization = `Bearer ${access}`;
          return api(originalRequest);
        }
      } catch (refreshError) {
        // Refresh failed, redirect to login
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        localStorage.removeItem('user');
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

export default api;
