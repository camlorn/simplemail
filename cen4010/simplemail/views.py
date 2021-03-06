from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
import pdb
from django.shortcuts import render
from django.db import transaction
from .mailgun_helper import *
from . import models
import email
import re
import django.contrib.auth as auth
import django.contrib.auth.models
from django.contrib.auth.decorators import login_required
import json
import simplemail.forms
import flanker.addresslib

def incoming_message(message_dict):
    #We first extract the message id, which is supposed to be unique for all e-mails everywhere.
    headers=message_dict['message-headers']
    #Nothing actually stops someone sending a message with multiple headers of the same type.
    ids=[j[1] for j in headers if j[0] == 'Message-Id']
    id=ids[0]
    #This ID may already be in the database. If it is, we bail out now.
    exists=models.Email.objects.filter(message_id= id).count()
    if exists >0:
        return False
    #The other pieces of important information we need out of the e-mail is the In-Reply-To header and the references header.
    #Together, these form a list of ids which we use to build threads, later.
    in_reply_to =[j[1] for j in headers if j[0]== 'In-Reply-To']
    references=[j[1] for j in headers if j[0]=='References']
    #The in_reply_to field is one id only so we start with that.
    in_thread = list(in_reply_to)
    for j in references:
        in_thread +=re.findall(r"<[^<]+>", j)
    in_thread= set(in_thread) #remove duplicates.
    #Handle users.
    user_emails= [j.strip() for j in message_dict['recipients'].split(",")]
    users=list(models.UserProfile.objects.filter(email__in = user_emails))
    #If the lengths do not match, we have a recipient we can't deliver to.
    #Mailgun only lets us handle all or fail all, so we properly need to send out notifications.
    if len(user_emails)> len(users):
        return False
    #We now have enough info to build the message itself as follows.
    new_message=models.Email(
        message_id=id,
        subject=message_dict['subject'],
        from_address=message_dict['from'],
        body =message_dict['body-plain'],
        body_stripped = message_dict['stripped-text'] if 'stripped-text' in message_dict else i['body-plain'],
        signature= message_dict['stripped-signature'] if 'stripped-signature' in message_dict else "",
    )
    #Build up a ist of all the e-mail addresses involved in this e-mail.
    all_addresses = message_dict['from']+","+message_dict['To']
    new_message.all_addresses= all_addresses
    new_message.save()
    for u in users:
        u.inbox.add(new_message)
        u.save()
    return True

@transaction.atomic
def get_all_mailgun_messages(request):
    result =mget("events").json()
    events=result['items']
    message_urls=[i['storage']['url'] for i in events if i['event']=='stored']
    messages_request = [mget(i, prepend=False) for i in message_urls]
    messages_json=[i.text for i in messages_request]
    count = 0
    #For each message, we make an e-mail model and save it.
    for i in messages_json:
        if incoming_message(json.loads(i)) == True:
            count +=1
    return render(request, 'simplemail/mailgun_got_messages.html', {'count': count})

@csrf_exempt
@transaction.atomic
def incoming_message_view(request):
    if request.method == "GET":
        return render(request, "simplemail/message.html", {'message': "This is for mailgun messages only."})
    else:
        d =dict(request.POST)
        print d['from'], d['To']
        d['subject'] =d['subject'][0]
        d['body-plain'] = d['body-plain'][0]
        d['stripped-text'] = d.get('stripped-text', [""])[0]
        d['stripped-signature'] = d.get('stripped-signature', [""])[0]
        d['message-headers'] = json.loads(d['message-headers'][0]) #Fix this. When posting, it's a string.
        d['recipients'] = ",".join(d['recipient'])
        d['from'] = ",".join(d['from'])
        d['To'] = ",".join(d['To'])
        res = incoming_message(d)
        if res:
            return HttpResponse("Message accepted", status=200)
        else:
            return HttpResponse("Message rejected", status=406)

#Create an account.
@transaction.atomic
def create_account(request):
    if request.method == 'GET':
        return simplemail.forms.render_account_creation_form(request)
    elif request.method== 'POST':
        form = simplemail.forms.AccountCreationForm(request.POST)
        if not form.is_valid():
            return simplemail.forms.render_account_creation_form(request, form = form)
        username=form.cleaned_data.get("user_name")
        password= form.cleaned_data.get("password")
        first_name= form.cleaned_data.get("first_name")
        last_name= form.cleaned_data.get("last_name")
        user = django.contrib.auth.models.User.objects.create_user(username=username, first_name=first_name, last_name=last_name, password= password)
        profile= models.UserProfile(email=username+"@simplemail.camlorn.net", first_name=first_name, last_name=last_name, user= user)
        user.save()
        profile.save()
        #Django makes us go through the login API "properly", so we do.
        u=auth.authenticate(username=username, password=password)
        auth.login(request, u)
        return render(request, "simplemail/message.html", {'message': "Your account was created successfully.  Your e-mail is {}@simplemail.camlorn.net".format(username)})

def show_folder(request, messages, folder_title, action_view= None, action_text=""):
    return render(request, "simplemail/folder.html", {
        'messages': messages,
        'folder_title': folder_title,
        'has_action': action_view is not None,
        'action_view': action_view,
        'action_text': action_text,
    })


@login_required
@transaction.atomic
def inbox(request):
    messages= request.user.profile.inbox.all().order_by('-date')
    return show_folder(request, messages, "Inbox", "delete_message", "Delete This Message")

@login_required
@transaction.atomic
def outbox(request):
    messages= request.user.profile.outbox.all().order_by('-date')
    return show_folder(request, messages, "Sent Messages", "delete_message", "Delete This Message")

@login_required
@transaction.atomic
def trash(request):
    messages= request.user.profile.trash.all().order_by('-date')
    return show_folder(request, messages, "Deleted Messages")

#all of the following views manipulate individual messages.
#This helper function tells us if a user owns a message.
def can_manipulate_message(profile, message):
    inbox = message.inbox_users.filter(pk=profile.pk).exists()
    outbox =message.outbox_users.filter(pk=profile.pk).exists()
    trash = message.trash_users.filter(pk=profile.pk).exists()
    return inbox or outbox or trash

@login_required
@transaction.atomic
def view_message(request, message_id):
    message= models.Email.objects.get(pk=message_id)
    if can_manipulate_message(request.user.profile, message):
        return render(request, "simplemail/view_message.html", {'message': message})
    else:
        return render(request, "simplemail/message.html", {'message': "You do not have permission to view this message."})

@login_required
@transaction.atomic
def send_message(request):
    if request.method== 'GET':
        return simplemail.forms.render_send_message_form(request)
    if request.method== 'POST':
        form=simplemail.forms.SendEmailForm(request.POST)
        if not form.is_valid():
            return simplemail.forms.render_send_message_form(request, form)
        #Okay, we can send the e-mail.
        to_addresses = [i.to_unicode() for i in form.cleaned_data['to']]
        subject=form.cleaned_data['subject']
        body = form.cleaned_data['message']
        result=send_email(request.user.profile.email, to_addresses, subject, body)
        if result.status_code!=200: #mailgun error.
            message= "Sorry, but something has gone wrong.  Please send us the following info:\n\n"+result.json()['message']
            return render(request, "simplemail/message.html", {'message': message})
        new_message_id= result.json()['id']
        new_message = models.Email.objects.create(
            message_id = new_message_id,
            subject= subject,
            from_address = request.user.profile.email,
            all_addresses = request.user.profile.email + "," + ",".join(to_addresses),
            to_addresses = ",".join(to_addresses),
            body= body,
            body_stripped=body,
            signature = "",
        )
        new_message.save()
        request.user.profile.outbox.add(new_message)
        request.user.profile.save()
        return render(request, "simplemail/message.html", {'message': "Your message has been sent."})

@login_required
@transaction.atomic
def reply_to_message(request, id):
    replying_to = models.Email.objects.get(pk = id)
    if not can_manipulate_message(request.user.profile, replying_to):
        return render(request, "simplemail/message.html", {'message': "You cannot reply to this message."})
    if request.method== 'GET':
        return simplemail.forms.render_reply_to_message_form(request, in_reply_to= replying_to)
    if request.method== 'POST':
        form=simplemail.forms.ReplyToEmailForm(request.POST)
        if not form.is_valid():
            return simplemail.forms.render_reply_to_message_form(request, form, in_reply_to=replying_to)
        #Okay, we can send the e-mail.
        to_addresses = [i.to_unicode() for i in form.cleaned_data['to']]
        subject=form.cleaned_data['subject']
        body = form.cleaned_data['message']
        result=send_email(request.user.profile.email, to_addresses, subject, body, in_reply_to=replying_to.message_id)
        if result.status_code!=200: #mailgun error.
            message= "Sorry, but something has gone wrong.  Please send us the following info:\n\n"+result.json()['message']
            return render(request, "simplemail/message.html", {'message': message})
        new_message_id= result.json()['id']
        new_message = models.Email.objects.create(
            message_id = new_message_id,
            subject= subject,
            from_address = request.user.profile.email,
            all_addresses = request.user.profile.email + "," + ",".join(to_addresses),
            to_addresses = ",".join(to_addresses),
            body= body,
            body_stripped=body,
            signature = "",
        )
        new_message.save()
        request.user.profile.outbox.add(new_message)
        request.user.profile.save()
        return render(request, "simplemail/message.html", {'message': "Your reply has been sent."})

@login_required
@transaction.atomic
def delete_message(request, id):
    message = models.Email.objects.get(pk = id)
    if not can_manipulate_message(request.user.profile, message):
        return render(request, "simplemail/message.html", {'message': "You cannot delete this message."})
    p=request.user.profile
    if p.trash.filter(pk=message.id).exists():
        return render(request, "simplemail/message.html", {'message': "This message was already deleted."})
    elif p.inbox.filter(pk=message.id).exists():
        p.inbox.remove(message)
        p.trash.add(message)
    elif p.outbox.filter(pk = message.id).exists():
        p.outbox.remove(message)
        p.trash.add(message)
        p.trash_sent.add(message)
    p.save()
    message.save()
    return render(request, "simplemail/message.html", {'message': "Your message was deleted."})
