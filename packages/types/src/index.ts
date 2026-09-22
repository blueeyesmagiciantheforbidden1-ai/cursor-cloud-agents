export interface Task {
  id: string;
  title: string;
  done: boolean;
  createdAt: string;
}

export interface HealthResponse {
  status: "ok";
  uptime: number;
}

export interface CreateTaskBody {
  title: string;
}

export interface UpdateTaskBody {
  title?: string;
  done?: boolean;
}

export interface ApiError {
  error: string;
}
