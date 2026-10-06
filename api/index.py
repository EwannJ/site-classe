const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

const supabasePublic = createClient(SUPABASE_URL || '', SUPABASE_ANON_KEY || '');
const supabaseAdmin = createClient(SUPABASE_URL || '', SUPABASE_SERVICE_ROLE_KEY || '');

async function getAuthContext(event) {
  const authHeader = event.headers.authorization || event.headers.Authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) return null;
  const token = authHeader.split(' ')[1];

  const { data: { user }, error } = await supabaseAdmin.auth.getUser(token);
  if (error || !user) return null;

  const { data: profile } = await supabaseAdmin.from('profiles').select('*').eq('id', user.id).single();
  return { user, profile, token };
}

exports.handler = async (event) => {
  const path = event.path.replace(/^\/api\/?/, '').replace(/^\/\.netlify\/functions\/api\/?/, '');
  const method = event.httpMethod;
  const body = event.body ? JSON.parse(event.body) : {};

  const json = (statusCode, data) => ({
    statusCode,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });

  try {
    if (path === 'auth/login' && method === 'POST') {
      const { email, password } = body;
      const { data, error } = await supabasePublic.auth.signInWithPassword({ email, password });
      if (error) return json(400, { error: error.message });

      const { data: profile } = await supabaseAdmin.from('profiles').select('*').eq('id', data.user.id).single();
      return json(200, { token: data.session.access_token, user: data.user, profile });
    }

    if (path === 'posts' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('posts').select('*').order('created_at', { ascending: false });
      if (error) return json(500, { error: error.message });
      return json(200, data);
    }

    if (path === 'sheets/approved' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('revision_sheets').select('*').eq('status', 'approved').order('created_at', { ascending: false });
      if (error) return json(500, { error: error.message });
      return json(200, data);
    }

    const auth = await getAuthContext(event);
    if (!auth) return json(401, { error: "Non autorisé. Veuillez vous connecter." });

    const { user, profile } = auth;
    const isAdmin = profile?.role === 'admin' || profile?.role === 'webmaster';
    const isWebmaster = profile?.role === 'webmaster';

    if (path === 'auth/me' && method === 'GET') {
      return json(200, { user, profile });
    }

    if (path === 'auth/change-password' && method === 'POST') {
      const { error } = await supabaseAdmin.auth.admin.updateUserById(user.id, { password: body.new_password });
      if (error) return json(400, { error: error.message });
      return json(200, { message: "Mot de passe mis à jour." });
    }

    if (path === 'results/my' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('student_results').select('*').eq('student_id', user.id).order('updated_at', { ascending: false });
      if (error) return json(500, { error: error.message });
      return json(200, data);
    }

    if (path === 'sheets/my' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('revision_sheets').select('*').eq('created_by', user.id).order('created_at', { ascending: false });
      if (error) return json(500, { error: error.message });
      return json(200, data);
    }

    if (path === 'sheets/upload' && method === 'POST') {
      const { title, subject, fileName, fileBase64 } = body;
      const buffer = Buffer.from(fileBase64.replace(/^data:.*;base64,/, ''), 'base64');
      const fileExt = fileName.split('.').pop();
      const storagePath = `${user.id}/${Date.now()}.${fileExt}`;

      const { error: uploadError } = await supabaseAdmin.storage.from('revision_files').upload(storagePath, buffer, {
        contentType: fileBase64.substring(fileBase64.indexOf(':') + 1, fileBase64.indexOf(';'))
      });
      if (uploadError) return json(500, { error: uploadError.message });

      const { data: { publicUrl } } = supabaseAdmin.storage.from('revision_files').getPublicUrl(storagePath);

      const { error: dbError } = await supabaseAdmin.from('revision_sheets').insert([{
        title, subject, file_url: publicUrl, file_name: fileName, created_by: user.id, status: 'pending', rejection_reason: null
      }]);
      if (dbError) return json(500, { error: dbError.message });

      return json(200, { message: "Fiche envoyée en modération." });
    }

    if (path === 'intake/my' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('delegate_intake_responses').select('*').eq('student_id', user.id);
      if (error) return json(500, { error: error.message });
      return json(200, data && data.length > 0 ? data[0] : null);
    }

    if (path === 'intake/submit' && method === 'POST') {
      const { info, has_whatsapp, whatsapp_handle, has_snapchat, snapchat_handle, has_instagram, instagram_handle } = body;
      const { error } = await supabaseAdmin.from('delegate_intake_responses').upsert({
        student_id: user.id,
        info,
        has_whatsapp,
        whatsapp_handle,
        has_snapchat,
        snapchat_handle,
        has_instagram,
        instagram_handle
      }, { onConflict: 'student_id' });

      if (error) return json(500, { error: error.message });
      return json(200, { message: "Réponses enregistrées avec succès." });
    }

    // --- NOUVELLES ROUTES : Mouvement Lycéen ---
    if (path === 'mouvement/my' && method === 'GET') {
      const { data, error } = await supabaseAdmin.from('mouvement_lyceen_responses').select('*').eq('student_id', user.id);
      if (error) return json(500, { error: error.message });
      return json(200, data && data.length > 0 ? data[0] : null);
    }

    if (path === 'mouvement/submit' && method === 'POST') {
      const { difficulties, improvements } = body;
      const { error } = await supabaseAdmin.from('mouvement_lyceen_responses').upsert({
        student_id: user.id,
        difficulties,
        improvements
      }, { onConflict: 'student_id' });

      if (error) return json(500, { error: error.message });
      return json(200, { message: "Réponses sur le mouvement lycéen enregistrées avec succès." });
    }

    if (isAdmin) {
      if (path === 'posts/create' && method === 'POST') {
        const { error } = await supabaseAdmin.from('posts').insert([{ title: body.title, content: body.content }]);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Annonce publiée." });
      }

      if (path === 'posts/update' && method === 'POST') {
        const { id, title, content } = body;
        const { error } = await supabaseAdmin.from('posts').update({ title, content }).eq('id', id);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Annonce mise à jour." });
      }

      if (path === 'posts/delete' && method === 'POST') {
        const { id } = body;
        const { error } = await supabaseAdmin.from('posts').delete().eq('id', id);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Annonce supprimée." });
      }

      if (path === 'sheets/pending' && method === 'GET') {
        const { data, error } = await supabaseAdmin.from('revision_sheets').select('*').eq('status', 'pending').order('created_at', { ascending: false });
        if (error) return json(500, { error: error.message });
        return json(200, data);
      }

      if (path === 'sheets/approve' && method === 'POST') {
        const { error } = await supabaseAdmin.from('revision_sheets').update({ status: 'approved', rejection_reason: null }).eq('id', body.sheetId);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Fiche approuvée." });
      }

      if (path === 'sheets/reject' && method === 'POST') {
        const { sheetId, reason } = body;
        const { error } = await supabaseAdmin.from('revision_sheets').update({ 
          status: 'rejected', 
          rejection_reason: reason || 'Aucune raison spécifiée.' 
        }).eq('id', sheetId);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Fiche refusée avec motif." });
      }

      if (path === 'sheets/delete' && method === 'POST') {
        const { error } = await supabaseAdmin.from('revision_sheets').delete().eq('id', body.sheetId);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Fiche supprimée." });
      }

      if (path === 'admin/students' && method === 'GET') {
        const { data, error } = await supabaseAdmin.from('profiles').select('id, email, full_name, role').neq('role', 'webmaster');
        if (error) return json(500, { error: error.message });
        return json(200, data);
      }

      if (path === 'admin/student-results' && method === 'POST') {
        const { data, error } = await supabaseAdmin.from('student_results').select('*').eq('student_id', body.studentId).order('updated_at', { ascending: false });
        if (error) return json(500, { error: error.message });
        return json(200, data);
      }

      if (path === 'admin/add-result' && method === 'POST') {
        const { error } = await supabaseAdmin.from('student_results').insert([{ 
          student_id: body.studentId, 
          title: body.title || 'Conseil de classe', 
          appreciation: body.appreciation 
        }]);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Résultat ajouté." });
      }

      if (path === 'intake/responses' && method === 'GET') {
        const { data: responses, error: respError } = await supabaseAdmin.from('delegate_intake_responses').select('*').order('created_at', { ascending: false });
        if (respError) return json(500, { error: respError.message });

        const { data: profiles, error: profError } = await supabaseAdmin.from('profiles').select('id, full_name, email');
        if (profError) return json(500, { error: profError.message });

        const profileMap = {};
        (profiles || []).forEach(p => { profileMap[p.id] = p; });

        const combined = (responses || []).map(r => ({
          ...r,
          profiles: profileMap[r.student_id] || { full_name: 'Élève inconnu', email: '' }
        }));

        return json(200, combined);
      }

      // --- NOUVELLE ROUTE ADMIN : Résultats Mouvement Lycéen ---
      if (path === 'mouvement/responses' && method === 'GET') {
        const { data: responses, error: respError } = await supabaseAdmin.from('mouvement_lyceen_responses').select('*').order('created_at', { ascending: false });
        if (respError) return json(500, { error: respError.message });

        const { data: profiles, error: profError } = await supabaseAdmin.from('profiles').select('id, full_name, email');
        if (profError) return json(500, { error: profError.message });

        const profileMap = {};
        (profiles || []).forEach(p => { profileMap[p.id] = p; });

        const combined = (responses || []).map(r => ({
          ...r,
          profiles: profileMap[r.student_id] || { full_name: 'Élève inconnu', email: '' }
        }));

        return json(200, combined);
      }
    }

    if (isWebmaster) {
      if (path === 'webmaster/create-user' && method === 'POST') {
        const { email, password, name, role } = body;
        const { data: newUser, error: createError } = await supabaseAdmin.auth.admin.createUser({
          email, password, email_confirm: true, user_metadata: { full_name: name }
        });
        if (createError) return json(400, { error: createError.message });

        await supabaseAdmin.from('profiles').update({ role }).eq('id', newUser.user.id);
        return json(200, { message: "Utilisateur créé avec succès." });
      }

      if (path === 'webmaster/profiles' && method === 'GET') {
        const { data, error } = await supabaseAdmin.from('profiles').select('*').order('email');
        if (error) return json(500, { error: error.message });
        return json(200, data);
      }

      if (path === 'webmaster/update-profile' && method === 'POST') {
        const { userId, name, role } = body;
        const { error } = await supabaseAdmin.from('profiles').update({ full_name: name, role }).eq('id', userId);
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Profil mis à jour." });
      }

      if (path === 'webmaster/reset-password' && method === 'POST') {
        const { userId, newPassword } = body;
        const { error } = await supabaseAdmin.auth.admin.updateUserById(userId, { password: newPassword });
        if (error) return json(500, { error: error.message });
        return json(200, { message: "Mot de passe réinitialisé." });
      }
    }

    return json(404, { error: "Route non trouvée ou privilèges insuffisants." });

  } catch (err) {
    return json(500, { error: err.message });
  }
};
